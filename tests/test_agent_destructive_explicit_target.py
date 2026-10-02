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

Four pieces now hold the rule, and the tests here hold them together:
- middleware/params_step.py _EXPLICIT_TARGET_CALLS: no injection for them;
- http_routes/access.py _resolve_http_bound_agent: the REST prebind binds the
  caller for the same set without writing it in as the target;
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
        ("agent", {"action": "archive"}),
        ("agent", {"action": "archive", "force": True}),
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


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("delete_agent", {"confirm": True}),
        ("archive_agent", {}),
        ("delete_agent", {"agent_id": TARGET, "confirm": True}),
        ("archive_agent", {"agent_id": TARGET}),
        ("delete_agent", {"kwargs": {"confirm": True}}),
    ],
)
def test_a_removed_destructive_alias_is_refused_before_identity_and_touches_nothing(name, arguments):
    """delete_agent and archive_agent were aliases of agent(action=...) until
    2026-09-28. A caller that still sends them must get TOOL_NOT_FOUND from
    the real dispatch pipeline before any identity step runs, and no agent
    may be archived or deleted, named or not."""
    from src.mcp_handlers import dispatch_tool

    resolve = AsyncMock(return_value={"agent_uuid": CALLER})
    with _Bound() as bound, patch(
        "src.mcp_handlers.identity.handlers.resolve_session_identity", resolve
    ):
        result = asyncio.run(dispatch_tool(name, dict(arguments)))
        payload = json.loads(result[0].text)
        assert payload.get("error_code") == "TOOL_NOT_FOUND", payload
        resolve.assert_not_awaited()
        assert bound.archived() == [] and bound.deleted() == []
        assert {m.status for m in bound.server.agent_metadata.values()} == {"active"}


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
            _dispatch("agent", {"action": "archive", "agent_id": CALLER})
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
# The refusals say truly where a UUID comes from
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments,code",
    [
        ({"action": "archive"}, "TARGET_AGENT_REQUIRED"),
        ({"action": "delete", "agent_id": "target-pub", "confirm": True}, "TARGET_AGENT_UUID_REQUIRED"),
        ({"action": "archive", "agent_id": UNREGISTERED}, "TARGET_AGENT_NOT_FOUND"),
    ],
)
def test_the_recovery_does_not_send_a_non_operator_to_list_for_a_uuid(arguments, code):
    """Codex on #2532: every refusal said agent(action='list') shows each
    agent's UUID. For a caller without operator credentials list shows its own
    UUID and a public handle for every other agent, and the handle it gives is
    refused here."""
    from src.mcp_handlers.lifecycle.query import _visible_agent_identifier

    with _Bound() as bound:
        meta = bound.server.agent_metadata
        # What list shows a non-operator caller, row by row.
        assert _visible_agent_identifier(
            CALLER, meta[CALLER], caller_uuid=CALLER, operator_caller=False
        ) == (CALLER, False)
        listed, redacted = _visible_agent_identifier(
            TARGET, meta[TARGET], caller_uuid=CALLER, operator_caller=False
        )
        assert redacted and listed != TARGET
        assert _visible_agent_identifier(
            TARGET, meta[TARGET], caller_uuid=CALLER, operator_caller=True
        ) == (TARGET, False)
        _sent, via_list = asyncio.run(
            _dispatch("agent", {"action": "archive", "agent_id": listed})
        )
        assert via_list.get("error_code") == "TARGET_AGENT_UUID_REQUIRED", via_list

        payload = json.loads(asyncio.run(handle_agent(dict(arguments)))[0].text)
        assert payload.get("error_code") == code, payload
        hint = payload["recovery"]["action"]
        assert "UUIDs only to operator callers" in hint, hint
        assert "Your own UUID is the uuid start_session returned" in hint, hint
        assert "shows each agent's UUID" not in hint, hint
        assert bound.archived() == [] and bound.deleted() == []


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


def test_the_router_examples_name_archive_targets_by_uuid():
    """action_router returns these examples when a caller omits the action,
    so an archive or delete example must not show a label or legacy key that
    the handler now refuses (TARGET_AGENT_UUID_REQUIRED)."""
    import re

    from src.mcp_handlers import consolidated

    source = open(consolidated.__file__).read()
    for example in re.findall(r"agent\(action='(?:archive|delete)'[^\n]*", source):
        target = re.search(r"agent_id='([^']*)'", example)
        assert target, example
        value = target.group(1)
        assert value.startswith("<") or re.fullmatch(r"[0-9a-f-]{36}", value), example


# ---------------------------------------------------------------------------
# REST: the prebind binds the caller and leaves the target unnamed
# ---------------------------------------------------------------------------


async def _dispatch_unwrapped(name: str, arguments: dict) -> tuple[dict, dict]:
    """REST dispatch unwraps a kwargs wrapper before the alias step."""
    from src.mcp_handlers.middleware import unwrap_kwargs

    name, arguments, _ctx = await unwrap_kwargs(
        name, dict(arguments), DispatchContext(bound_agent_id=CALLER)
    )
    return await _dispatch(name, arguments)


def _rest_prebind(name: str, arguments: dict, path: str, monkeypatch) -> tuple[str | None, dict]:
    """Run the real REST prebind with the caller resolved on one path.

    The operator path runs as written, with only the operator lookup stubbed.
    The sticky and session paths are stood in for by fakes that stamp the way
    the real ones do, through _preserve_explicit_target (the structural lock
    in test_http_prebind_preserves_target.py keeps every path on that helper).
    """
    import src.http_routes.access as access

    async def operator(_signals):
        return {"agent_uuid": CALLER} if path == "operator" else None

    async def sticky(args, _signals):
        if path != "sticky":
            return None, None
        access._preserve_explicit_target(args, CALLER)
        return CALLER, None

    async def session(_tool_name, args, _signals, _consult):
        access._preserve_explicit_target(args, CALLER)
        return CALLER

    monkeypatch.setattr(
        "src.mcp_handlers.identity.operator.resolve_operator_identity", operator
    )
    monkeypatch.setattr(access, "_consult_http_sticky_binding", sticky)
    monkeypatch.setattr(access, "_resolve_http_session_binding", session)
    sent = dict(arguments)
    bound = asyncio.run(access._resolve_http_bound_agent(name, sent, object()))
    return bound, sent


@pytest.mark.parametrize("path", ["operator", "sticky", "session"])
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("agent", {"action": "delete", "confirm": True}),
        ("agent", {"op": "delete", "confirm": True}),
        ("agent", {"action": "archive"}),
        ("agent", {"action": "archive", "agent_id": ""}),
        # Codex on #2579: the kwargs wrapper REST accepts. The stamp lands on
        # the outer dict and dispatch unwraps kwargs over it afterwards.
        ("agent", {"kwargs": {"action": "delete", "confirm": True}}),
        ("agent", {"kwargs": json.dumps({"action": "archive"})}),
    ],
)
def test_rest_prebind_does_not_name_the_caller_as_the_target(name, arguments, path, monkeypatch):
    """Every REST prebind path stamps the resolved caller into an omitted
    agent_id. For archive and delete that brought back, over REST, the default
    #2532 removed on /mcp/: agent(action='delete', confirm=true) with no
    agent_id deleted the caller. The caller is still bound; the call still
    names no target, and is refused."""
    bound_to, sent = _rest_prebind(name, arguments, path, monkeypatch)
    assert bound_to == CALLER, "the prebind still binds the caller"
    assert not sent.get("agent_id"), f"the prebind named the caller as the target: {sent}"
    with _Bound() as bound:
        _dispatched, payload = asyncio.run(_dispatch_unwrapped(name, sent))
        assert payload.get("error_code") == "TARGET_AGENT_REQUIRED", payload
        assert bound.archived() == [] and bound.deleted() == []
        assert bound.server.agent_metadata[CALLER].status == "active"


@pytest.mark.parametrize("path", ["operator", "sticky", "session"])
@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "archive", "agent_id": TARGET},
        {"kwargs": {"action": "archive", "agent_id": TARGET}},
    ],
)
def test_rest_prebind_keeps_a_named_target(arguments, path, monkeypatch):
    from src.mcp_handlers.middleware.params_step import unwrapped_view

    _bound_to, sent = _rest_prebind("agent", arguments, path, monkeypatch)
    assert unwrapped_view(sent)["agent_id"] == TARGET


@pytest.mark.parametrize("path", ["operator", "sticky", "session"])
def test_rest_prebind_reads_a_nested_kwargs_wrapper(path, monkeypatch):
    """A wrapper inside a wrapper: dispatch unwraps one level and a handler
    may unwrap the next, so the outer dict still gets no target."""
    _bound_to, sent = _rest_prebind(
        "agent",
        {"kwargs": {"kwargs": json.dumps({"action": "delete", "confirm": True})}},
        path,
        monkeypatch,
    )
    assert "agent_id" not in sent


@pytest.mark.parametrize("path", ["operator", "sticky", "session"])
@pytest.mark.parametrize(
    "arguments",
    [
        # wrapper form: the destructive action only exists inside kwargs
        {"kwargs": {"action": "delete", "confirm": True}},
        {"kwargs": json.dumps({"action": "delete", "confirm": True})},
        # a deeper ``get`` must not cancel an outer delete (dispatch unwraps twice)
        {
            "action": "delete",
            "confirm": True,
            "kwargs": {"kwargs": {"kwargs": {"action": "get"}}},
        },
    ],
)
def test_rest_prebind_reads_every_unwrap_depth(arguments, path, monkeypatch):
    _bound_to, sent = _rest_prebind("agent", arguments, path, monkeypatch)
    assert "agent_id" not in sent


@pytest.mark.parametrize("path", ["operator", "sticky", "session"])
def test_rest_prebind_ignores_a_wrapper_dispatch_never_unwraps(path, monkeypatch):
    """A destructive call 3 wrappers deep is inert data: it must not strip the
    target of a valid outer ``get``."""
    _bound_to, sent = _rest_prebind(
        "agent",
        {
            "action": "get",
            "agent_id": TARGET,
            "kwargs": {"kwargs": {"kwargs": {"action": "delete", "agent_id": ""}}},
        },
        path,
        monkeypatch,
    )
    assert sent["agent_id"] == TARGET


def test_unwrapped_view_depth_zero_is_the_outer_dict():
    from src.mcp_handlers.middleware.params_step import unwrapped_view

    nested = {"a": 1, "kwargs": {"a": 2, "kwargs": {"a": 3}}}
    assert unwrapped_view(nested, 0) == {"a": 1}
    assert unwrapped_view(nested, 1) == {"a": 2}
    assert unwrapped_view(nested, 2) == {"a": 3}


def test_unwrapped_view_merges_like_unwrap_kwargs():
    from src.mcp_handlers.middleware.params_step import unwrapped_view

    assert unwrapped_view({"a": 1, "kwargs": {"a": 2, "b": 3}}) == {"a": 2, "b": 3}
    assert unwrapped_view({"a": 1, "kwargs": json.dumps({"b": 2})}) == {"a": 1, "b": 2}
    assert unwrapped_view({"a": 1, "kwargs": "{not json"}) == {"a": 1}
    original = {"kwargs": {"agent_id": TARGET}}
    unwrapped_view(original)
    assert original == {"kwargs": {"agent_id": TARGET}}, "the view is a copy"


@pytest.mark.parametrize("path", ["operator", "sticky", "session"])
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("agent", {"action": "get"}),
        ("agent", {"action": "resume"}),
        ("get_governance_metrics", {}),
    ],
)
def test_rest_prebind_still_defaults_other_calls_to_the_caller(name, arguments, path, monkeypatch):
    """Regression guard: a self-scoped call keeps the session default."""
    _bound_to, sent = _rest_prebind(name, arguments, path, monkeypatch)
    assert sent["agent_id"] == CALLER

