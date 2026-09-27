"""archive and delete act only on the agent a call names.

Observed 2026-09-27 against origin/master: dispatch's inject_identity wrote the
session's own id into agent_id for every non-browsable call, so
agent(action='delete', confirm=true) with no agent_id deleted the CALLER. And a
named target the server could not resolve (a typo, a label it does not hold)
reached require_registered_agent, which falls back to the bound agent, so
archive(agent_id='<typo>') archived the caller too; and a label or public id
resolved to the first cached holder, although public_agent_id is shared by most
identities that carry one (src/mcp_handlers/dialectic/auth.py), so a shared
handle could delete an arbitrary agent. describe_tool's lite view
of both actions hid agent_id (LITE_IDENTITY_FIELDS), which is how a caller
reading it came to send the call without one.

Three pieces now hold the rule, and the last test here holds them together:
- middleware/params_step.py _EXPLICIT_TARGET_CALLS: no injection for them;
- lifecycle/mutation.py: refuse a call that names no target, and accept only
  the target's own id (a UUID), never a label or public id;
- AgentParams.ACTION_REQUIRED_FIELDS: lite lists agent_id as required at call
  time.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

import src.mcp_handlers  # noqa: F401
import src.mcp_handlers.consolidated  # noqa: F401

from src.mcp_handlers.consolidated import handle_agent
from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
from src.mcp_handlers.middleware import DispatchContext, inject_identity, resolve_alias
from src.mcp_handlers.middleware.params_step import _EXPLICIT_TARGET_CALLS
from src.mcp_handlers.schemas.lifecycle import AgentParams
from tests.helpers import (
    make_agent_meta,
    make_mock_server,
    patch_agent_storage,
    patch_lifecycle_server,
)

CALLER = "11111111-1111-4111-8111-111111111111"
TARGET = "2222abcd-2222-4222-8222-22222222abcd"
# A well-formed UUID that no agent holds.
UNREGISTERED = "44444444-4444-4444-8444-444444444444"


def _server():
    server = make_mock_server()
    server.agent_metadata = {
        CALLER: make_agent_meta(label="caller-label", public_agent_id="caller-pub"),
        TARGET: make_agent_meta(label="target-label", public_agent_id="target-pub"),
    }
    for uuid, meta in server.agent_metadata.items():
        meta.agent_uuid = uuid
    return server


class _Bound:
    """A session bound as CALLER, with every write the handlers make mocked."""

    def __init__(self):
        self.server = _server()
        self._stack = ExitStack()

    def __enter__(self):
        s = self._stack
        s.enter_context(patch_lifecycle_server(self.server))
        self.storage = s.enter_context(patch_agent_storage())
        self.storage.delete_agent = AsyncMock()
        self.storage.update_agent = AsyncMock()
        s.enter_context(
            patch("src.mcp_handlers.context.get_context_agent_id", return_value=CALLER)
        )
        s.enter_context(
            patch("src.mcp_handlers.shared.get_mcp_server", return_value=self.server)
        )
        s.enter_context(
            patch(
                "src.mcp_handlers.lifecycle.helpers.manual_archive_liveness_signals",
                AsyncMock(return_value=[]),
            )
        )
        self.archive = s.enter_context(
            patch(
                "src.mcp_handlers.lifecycle.helpers._archive_one_agent",
                AsyncMock(return_value=True),
            )
        )
        s.enter_context(
            patch(
                "src.mcp_handlers.lifecycle.mutation._invalidate_agent_cache",
                AsyncMock(),
            )
        )
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)

    def archived(self) -> list[str]:
        return [c.args[0] for c in self.archive.await_args_list]

    def deleted(self) -> list[str]:
        return [c.args[0] for c in self.storage.delete_agent.await_args_list]


async def _dispatch(name: str, arguments: dict) -> tuple[dict, dict]:
    """The call's real alias and identity steps, then the agent router."""
    ctx = DispatchContext(bound_agent_id=CALLER)
    step = await resolve_alias(name, dict(arguments), ctx)
    assert not isinstance(step, list), step
    name, arguments, ctx = step
    step = await inject_identity(name, arguments, ctx)
    assert not isinstance(step, list), step
    name, arguments, ctx = step
    result = await handle_agent(arguments)
    return arguments, json.loads(result[0].text)


# ---------------------------------------------------------------------------
# No target named: refused, and nothing is touched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("agent", {"action": "delete", "confirm": True}),
        ("agent", {"op": "delete", "confirm": True}),
        ("delete_agent", {"confirm": True}),
        ("agent", {"action": "archive"}),
        ("agent", {"action": "archive", "force": True}),
        ("archive_agent", {}),
    ],
)
def test_a_destructive_call_without_agent_id_does_not_reach_the_caller(name, arguments):
    with _Bound() as bound:
        sent, payload = asyncio.run(_dispatch(name, arguments))
        assert "agent_id" not in sent, "dispatch wrote the session's id in"
        assert payload.get("success") is False, payload
        assert payload.get("error_code") == "TARGET_AGENT_REQUIRED", payload
        recovery = payload.get("recovery") or {}
        assert "agent_id" in recovery.get("action", ""), recovery
        assert bound.archived() == [] and bound.deleted() == []
        assert bound.server.agent_metadata[CALLER].status == "active"


@pytest.mark.parametrize("action", ["archive", "delete"])
@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_agent_id_is_no_target(action, blank):
    with _Bound() as bound:
        arguments = {"action": action, "agent_id": blank, "confirm": True}
        payload = json.loads(asyncio.run(handle_agent(arguments))[0].text)
        assert payload.get("error_code") == "TARGET_AGENT_REQUIRED", payload
        assert bound.archived() == [] and bound.deleted() == []


# ---------------------------------------------------------------------------
# A name that does not resolve is refused, not re-pointed at the caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["archive", "delete"])
def test_an_unresolvable_agent_id_is_refused_rather_than_resolved_to_the_caller(action):
    with _Bound() as bound:
        _sent, payload = asyncio.run(
            _dispatch("agent", {"action": action, "agent_id": UNREGISTERED, "confirm": True})
        )
        assert payload.get("success") is False, payload
        assert payload.get("error_code") == "TARGET_AGENT_NOT_FOUND", payload
        assert UNREGISTERED in payload.get("error", ""), payload
        assert bound.archived() == [] and bound.deleted() == []
        assert bound.server.agent_metadata[CALLER].status == "active"


# ---------------------------------------------------------------------------
# A named target still works: another agent, or yourself by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("named", [TARGET, TARGET.upper(), f"{{{TARGET}}}"])
def test_archive_acts_on_the_agent_whose_uuid_is_named(named):
    with _Bound() as bound:
        _sent, payload = asyncio.run(
            _dispatch("agent", {"action": "archive", "agent_id": named})
        )
        assert payload.get("success") is True, payload
        assert bound.archived() == [TARGET]


def test_archiving_yourself_needs_your_own_uuid():
    with _Bound() as bound:
        _sent, payload = asyncio.run(
            _dispatch("archive_agent", {"agent_id": CALLER})
        )
        assert payload.get("success") is True, payload
        assert bound.archived() == [CALLER]


@pytest.mark.parametrize("action", ["archive", "delete"])
@pytest.mark.parametrize(
    "named", ["target-label", "target-pub", "caller-label", "caller-pub"]
)
def test_a_label_or_public_id_never_selects_a_target(action, named):
    """Not even the caller's own: a handle is not an identity."""
    with _Bound() as bound:
        _sent, payload = asyncio.run(
            _dispatch("agent", {"action": action, "agent_id": named, "confirm": True})
        )
        assert payload.get("success") is False, payload
        assert payload.get("error_code") == "TARGET_AGENT_UUID_REQUIRED", payload
        assert "UUID" in (payload.get("recovery") or {}).get("action", ""), payload
        assert bound.archived() == [] and bound.deleted() == []


def test_a_shared_public_id_cannot_delete_whichever_holder_is_cached_first():
    """The reviewer's case on #2532: most public ids have many holders."""
    third = "33333333-3333-4333-8333-333333333333"
    with _Bound() as bound:
        metadata = bound.server.agent_metadata
        metadata[third] = make_agent_meta(label="third", public_agent_id="shared-handle")
        metadata[third].agent_uuid = third
        metadata[TARGET].public_agent_id = "shared-handle"
        _sent, payload = asyncio.run(
            _dispatch(
                "agent",
                {"action": "delete", "agent_id": "shared-handle", "confirm": True},
            )
        )
        assert payload.get("error_code") == "TARGET_AGENT_UUID_REQUIRED", payload
        assert bound.deleted() == []
        assert {m.status for m in metadata.values()} == {"active"}


def test_delete_acts_on_the_agent_named():
    with _Bound() as bound:
        _sent, payload = asyncio.run(
            _dispatch(
                "agent",
                {"action": "delete", "agent_id": TARGET, "confirm": True, "backup_first": False},
            )
        )
        assert payload.get("success") is True, payload
        assert bound.deleted() == [TARGET]
        assert bound.server.agent_metadata[CALLER].status == "active"


def test_a_legacy_non_uuid_key_is_no_target_either():
    """The second review round on #2532: a legacy row is keyed by its old
    agent_id, which can equal a handle other agents share, so being the cache
    key is not being unique. 312 of 313 such rows were archived on 2026-09-27.
    """
    with _Bound() as bound:
        metadata = bound.server.agent_metadata
        metadata["legacy-handle"] = make_agent_meta(
            label="legacy", public_agent_id="legacy-handle"
        )
        metadata["legacy-handle"].agent_uuid = "legacy-handle"
        metadata[TARGET].public_agent_id = "legacy-handle"
        for action in ("archive", "delete"):
            _sent, payload = asyncio.run(
                _dispatch(
                    "agent",
                    {"action": action, "agent_id": "legacy-handle", "confirm": True},
                )
            )
            assert payload.get("error_code") == "TARGET_AGENT_UUID_REQUIRED", payload
        assert bound.archived() == [] and bound.deleted() == []
        assert {m.status for m in metadata.values()} == {"active"}


# ---------------------------------------------------------------------------
# Reads and self-scoped actions keep their session default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["get", "update", "resume"])
def test_non_destructive_actions_still_receive_the_session_id(action):
    ctx = DispatchContext(bound_agent_id=CALLER)
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=CALLER):
        _name, arguments, _ctx = asyncio.run(
            inject_identity("agent", {"action": action}, ctx)
        )
    assert arguments.get("agent_id") == CALLER


# ---------------------------------------------------------------------------
# describe_tool's lite view names the target
# ---------------------------------------------------------------------------


def _lite(**arguments) -> list[str]:
    payload = json.loads(
        asyncio.run(handle_describe_tool({"lite": True, **arguments}))[0].text
    )
    return payload["parameters"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"tool_name": "agent", "action": "archive"},
        {"tool_name": "agent", "action": "delete"},
        {"tool_name": "archive_agent"},
        {"tool_name": "delete_agent"},
    ],
)
def test_lite_view_of_a_destructive_action_requires_agent_id(arguments):
    assert "agent_id (required at call time)" in _lite(**arguments)


def test_lite_view_of_delete_requires_confirm_too():
    assert "confirm (required at call time)" in _lite(tool_name="agent", action="delete")


@pytest.mark.parametrize(
    "arguments",
    [
        {"tool_name": "agent", "action": "get"},
        {"tool_name": "agent", "action": "resume"},
        {"tool_name": "get_agent_metadata"},
    ],
)
def test_lite_view_of_a_targeted_read_shows_agent_id(arguments):
    assert "agent_id: string" in _lite(**arguments)


def test_lite_view_of_update_and_list_do_not_offer_a_target():
    """update writes only the caller's record; list and release_presence take none."""
    for action in ("update", "list", "release_presence"):
        names = {line.split(":")[0].split(" (")[0] for line in _lite(tool_name="agent", action=action)}
        assert "agent_id" not in names, (action, names)


# ---------------------------------------------------------------------------
# The three declarations agree
# ---------------------------------------------------------------------------


def test_no_injection_exactly_where_agent_id_is_required_at_call_time():
    """The middleware set and the schema declaration name the same actions.

    Both directions matter: an action that requires agent_id but still gets
    the session's id injected is refused by nothing and defaults to the
    caller; an action exempted from injection without declaring the
    requirement shows no agent_id in lite, and its callers learn only from
    the refusal.
    """
    required = {
        action
        for action, names in AgentParams.ACTION_REQUIRED_FIELDS.items()
        if "agent_id" in names
    }
    exempt = {
        action
        for action in AgentParams.ACTION_FIELDS
        if _EXPLICIT_TARGET_CALLS.matches("agent", {"action": action})
    }
    assert required == exempt == {"archive", "delete"}


_PROBE = {"agent_id": TARGET, "confirm": True, "backup_first": False}
_DECLARED = sorted(
    (action, name)
    for action, names in AgentParams.ACTION_REQUIRED_FIELDS.items()
    for name in names
)


@pytest.mark.parametrize("action,name", _DECLARED, ids=[f"{a}-{n}" for a, n in _DECLARED])
def test_each_declared_requirement_is_refused_when_missing(action, name):
    """Soundness, as for knowledge and dialectic in
    tests/test_describe_lite_action_parameters.py."""
    with _Bound() as bound:
        arguments = {"action": action}
        for other in AgentParams.ACTION_REQUIRED_FIELDS[action]:
            if other != name:
                arguments[other] = _PROBE[other]
        payload = json.loads(asyncio.run(handle_agent(arguments))[0].text)
        assert payload.get("success") is False, payload
        assert name in payload.get("error", ""), payload
        assert bound.archived() == [] and bound.deleted() == []
