"""A timed-out write reports an unknown outcome, never a failure to retry.

Observed live 2026-09-27 on the AGE backend: update_finding with a ~3 KB
resolution_notes returned ``Tool 'update_discovery_status_graph' timed out
after 10.0 seconds`` with the recovery "Try again with simpler parameters",
although the row had committed. resolution_notes append, so following that
recovery writes the notes twice.

The decorator's timeout cancels the await, not the work, in two ways:

* the handler is already past its commit. The AGE update refreshed the
  embedding after the transaction, and the first embedding in a process
  loads the model;
* the write is still running on the ExecutorPool's own loop thread, where
  cancelling the caller's await does not stop a statement the server is
  executing.

Both are reproduced here through the real update handler body.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.mcp_handlers  # noqa: F401  registers every tool and router
from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.decorators import (
    _ROUTER_ACTION_HANDLERS,
    _TOOL_DEFINITIONS,
    _is_first_party_module,
    mcp_tool,
    resolve_call_operation,
)
from src.mcp_handlers.error_helpers import _call_literal


DISCOVERY_ID = "2026-09-26T11:20:10.803820+00:00"
NOTES = "Confirmed on the live server; the fix is deployed."
READ_RECOVERY = "Try again with simpler parameters or check system health."


def _payload(result) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


def _discovery() -> DiscoveryNode:
    return DiscoveryNode(
        id=DISCOVERY_ID,
        agent_id="writer",
        type="bug_found",
        summary="a finding",
        details="x" * 11_000,
        severity="low",
        status="open",
    )


class _Graph:
    """Holds one row; ``commit`` applies an update the way storage would."""

    def __init__(self):
        self.row = _discovery()
        self.committed_at: float | None = None

    async def get_discovery(self, discovery_id):
        return self.row if discovery_id == self.row.id else None

    def commit(self, updates):
        self.row = replace(
            self.row,
            details=updates.get("details", self.row.details),
            updated_at=updates.get("updated_at", self.row.updated_at),
        )
        self.committed_at = time.monotonic()


def _short_timeout_update_handler(timeout: float):
    """The real update handler body under the decorator, with a short timeout.

    Wrapped through a local function so the decorator's attribute writes land
    on it, not on the shared handler.
    """
    from src.mcp_handlers.knowledge import handlers as kg_handlers

    body = kg_handlers.handle_update_discovery_status_graph.__wrapped__

    async def _update(arguments):
        return await body(arguments)

    return mcp_tool("update_discovery_status_graph", timeout=timeout, register=False)(
        _update
    )


def _update_arguments() -> dict:
    return {
        "action": "update",
        "agent_id": "unregistered",
        "discovery_id": DISCOVERY_ID,
        "resolution_notes": NOTES,
    }


def _patched_handler_dependencies(graph):
    server = MagicMock()
    server.agent_metadata = {}
    return (
        patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
        patch("src.mcp_handlers.shared.get_mcp_server", return_value=server),
        patch("src.mcp_handlers.knowledge.handlers.mcp_server", server),
        patch(
            "src.mcp_handlers.knowledge.handlers.get_knowledge_graph",
            new_callable=AsyncMock,
            return_value=graph,
        ),
        patch("src.mcp_handlers.knowledge.handlers.record_ms"),
        patch("src.coordination_failure_emit.emit_coordination_failure_sync"),
    )


async def _run_patched(handler, graph, arguments):
    patches = _patched_handler_dependencies(graph)
    for p in patches:
        p.start()
    try:
        return await handler(arguments)
    finally:
        for p in reversed(patches):
            p.stop()


def _assert_unknown_outcome_for_update(payload: dict) -> None:
    assert payload["success"] is False
    assert payload["error_code"] == "TIMEOUT"
    assert payload["error_category"] == "system_error"
    assert payload["outcome"] == "unknown"
    assert payload["operation"] == "write"
    assert "may have been saved" in payload["error"]
    assert "call_started_at" in payload
    _assert_settled_by_bounds_a_running_statement(payload)
    recovery = payload["recovery"]
    check = f"knowledge(action='details', discovery_id='{DISCOVERY_ID}')"
    assert recovery["check_before_retry"] == check
    assert check in recovery["action"]
    assert "updated_at" in recovery["action"]
    assert "try again" not in recovery["action"].lower()
    assert recovery["action"].startswith("Do not send this update again yet")


def _assert_settled_by_bounds_a_running_statement(payload: dict) -> None:
    """settled_by is the reply time plus the pool's command timeout."""
    from src.db.postgres_backend import COMMAND_TIMEOUT_SECONDS

    settled = datetime.fromisoformat(payload["settled_by"])
    replied = datetime.fromisoformat(payload["server_time"])
    margin = (settled - replied).total_seconds()
    assert COMMAND_TIMEOUT_SECONDS - 1 <= margin <= COMMAND_TIMEOUT_SECONDS + 1


@pytest.mark.asyncio
async def test_update_committed_before_a_slow_post_commit_step_reports_unknown_outcome():
    """The live shape: the row commits, a post-commit step outlasts the timeout."""
    graph = _Graph()

    async def update_discovery(discovery_id, updates):
        graph.commit(updates)
        await asyncio.sleep(5)  # stands in for the first-use model load
        return True

    graph.update_discovery = update_discovery
    handler = _short_timeout_update_handler(0.2)

    payload = _payload(await _run_patched(handler, graph, _update_arguments()))

    assert graph.committed_at is not None, "premise: the write committed"
    assert graph.row.details.endswith(NOTES), "premise: the notes are stored"
    _assert_unknown_outcome_for_update(payload)
    # The check the recovery prescribes tells this call's write apart.
    assert datetime.fromisoformat(graph.row.updated_at) >= datetime.fromisoformat(
        payload["call_started_at"]
    )


@pytest.mark.asyncio
async def test_update_still_running_on_the_executor_loop_commits_after_the_timeout():
    """The ExecutorPool shape: the caller's await is cancelled while the pool's
    loop thread is inside the statement, and the write lands after the reply.
    """
    from src.db.executor_pool import _await_on_loop

    graph = _Graph()
    pool_loop = asyncio.new_event_loop()
    pool_thread = threading.Thread(target=pool_loop.run_forever, daemon=True)
    pool_thread.start()

    async def update_discovery(discovery_id, updates):
        async def statement_on_pool_loop():
            time.sleep(0.5)  # the server executing the UPDATE and COMMIT
            graph.commit(updates)
            return True

        return await _await_on_loop(statement_on_pool_loop, pool_loop)

    graph.update_discovery = update_discovery
    handler = _short_timeout_update_handler(0.1)

    try:
        payload = _payload(await _run_patched(handler, graph, _update_arguments()))
        replied_at = time.monotonic()
        assert graph.committed_at is None, "premise: the reply came before the commit"

        deadline = time.monotonic() + 3.0
        while graph.committed_at is None and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
    finally:
        pool_loop.call_soon_threadsafe(pool_loop.stop)
        pool_thread.join(timeout=2.0)
        pool_loop.close()

    assert graph.committed_at is not None and graph.committed_at > replied_at, (
        "the cancelled await did not stop the write on the pool loop"
    )
    assert graph.row.details.endswith(NOTES)
    _assert_unknown_outcome_for_update(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name", ["get_discovery_details", "search_knowledge_graph", "health_check"]
)
async def test_read_tool_timeout_keeps_the_retry_recovery(tool_name):
    @mcp_tool(tool_name, timeout=0.05, register=False)
    async def _slow(arguments):
        await asyncio.sleep(5)

    with patch("src.coordination_failure_emit.emit_coordination_failure_sync"):
        payload = _payload(await _slow({}))

    assert payload["error"] == f"Tool '{tool_name}' timed out after 0.05 seconds."
    assert payload["error_code"] == "TIMEOUT"
    assert payload["error_category"] == "system_error"
    assert payload["recovery"] == {
        "action": READ_RECOVERY,
        "related_tools": ["health_check"],
    }
    assert "outcome" not in payload


STORE_CHECK = "knowledge(action='search', query='<words from your summary>')"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "writer",
    [
        "agent-1",
        # The low-friction path writes a pseudonym into arguments before the
        # store; it is not a registered agent, so a check that looks the writer
        # up (knowledge get by agent_id) would refuse this caller.
        "anonkg_mcp_0123456789ab",
    ],
)
async def test_store_timeout_names_a_search_any_caller_can_run(writer):
    @mcp_tool("store_knowledge_graph", timeout=0.05, register=False)
    async def _slow(arguments):
        await asyncio.sleep(5)

    with patch("src.coordination_failure_emit.emit_coordination_failure_sync"):
        payload = _payload(await _slow({"action": "store", "agent_id": writer}))

    assert payload["outcome"] == "unknown"
    recovery = payload["recovery"]
    assert recovery["check_before_retry"] == STORE_CHECK
    assert writer not in json.dumps(recovery)
    assert "new row" in recovery["action"]
    assert "try again" not in recovery["action"].lower()


def test_update_recovery_does_not_take_a_moved_updated_at_as_proof():
    """Another writer can move updated_at after this call starts; only the
    caller's own fields show that this call's write landed."""
    from src.mcp_handlers.decorators import CallOperation
    from src.mcp_handlers.error_helpers import _unknown_outcome_recovery

    recovery = _unknown_outcome_recovery(
        CallOperation(operation="write", tool="knowledge", action="update"),
        {"discovery_id": DISCOVERY_ID},
    )
    steps = " ".join(recovery["workflow"])
    assert "by this call or another" in recovery["action"]
    assert "earlier than call_started_at" in steps
    assert "your resolution_notes at the end of details" in steps
    assert "pagination.total_length" in steps
    assert "another writer changed it" in steps


@pytest.mark.parametrize(
    "call",
    [
        ("knowledge", "update"),
        ("knowledge", "store"),
        ("leave_note", None),
        ("process_agent_update", None),
    ],
)
def test_no_recovery_resends_before_the_running_statement_has_settled(call):
    """A statement still running at the timeout can commit for up to the pool's
    command timeout; a resend before then can land beside it."""
    from src.mcp_handlers.decorators import CallOperation
    from src.mcp_handlers.error_helpers import _unknown_outcome_recovery

    tool, action = call
    recovery = _unknown_outcome_recovery(
        CallOperation(operation="write", tool=tool, action=action),
        {"discovery_id": DISCOVERY_ID},
    )
    resend_steps = [
        step
        for step in recovery["workflow"]
        if re.search(r"(?<!not )(send|store) (the update|your update|the call|it) again", step)
    ]
    assert resend_steps, "premise: the workflow says when to send again"
    for step in resend_steps:
        assert "after settled_by" in step, step
    assert "few seconds" not in json.dumps(recovery)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["onboard", "identity"])
async def test_identity_minting_timeout_never_gets_the_retry_reply(tool_name):
    """The catalog labels onboard and identity read, but both can create an
    identity, and a repeat creates another."""
    @mcp_tool(tool_name, timeout=0.05, register=False)
    async def _slow(arguments):
        await asyncio.sleep(5)

    with patch("src.coordination_failure_emit.emit_coordination_failure_sync"):
        payload = _payload(await _slow({"force_new": True}))

    assert payload["outcome"] == "unknown"
    assert payload["operation"] == "read", "the catalog label is reported as it is"
    assert READ_RECOVERY not in json.dumps(payload)
    assert "creat" in payload["recovery"]["action"]
    assert "start_session" in payload["recovery"]["related_tools"]


@pytest.mark.parametrize("tool", ["onboard", "identity"])
def test_a_call_naming_its_binding_is_told_to_read_it_not_to_mint(tool):
    """A resume with a client_session_id must not be pushed into a fresh
    identity; reading the named binding settles it."""
    from src.mcp_handlers.decorators import CallOperation
    from src.mcp_handlers.error_helpers import _unknown_outcome_recovery

    recovery = _unknown_outcome_recovery(
        CallOperation(operation="read", tool=tool, mints_identity=True),
        {"client_session_id": "sess-1"},
    )

    assert recovery["check_before_retry"] == "identity(client_session_id='sess-1')"
    assert recovery["workflow"][0] == "1. Call identity(client_session_id='sess-1')"
    assert "after settled_by" in recovery["workflow"][2]


@pytest.mark.parametrize(
    "arguments",
    [
        {"force_new": True},
        {"force_new": True, "client_session_id": "sess-1"},
        {"client_session_id": "it's"},
    ],
)
def test_a_fresh_or_unproven_onboard_is_told_a_repeat_creates_another(arguments):
    from src.mcp_handlers.decorators import CallOperation
    from src.mcp_handlers.error_helpers import _unknown_outcome_recovery

    recovery = _unknown_outcome_recovery(
        CallOperation(operation="read", tool="onboard", mints_identity=True),
        arguments,
    )

    assert recovery["check_before_retry"] is None
    assert "every further call creates another" in recovery["action"]
    assert "do not retry in a loop" in recovery["action"]


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("onboard", {"force_new": True}),
        ("start_session", {"force_new": True}),
        ("identity", {}),
    ],
)
def test_identity_minting_calls_are_not_retry_safe(tool_name, arguments):
    call = resolve_call_operation(tool_name, arguments)
    assert call.mints_identity is True
    assert call.retry_safe is False


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_discovery_details", {}),
        ("knowledge", {"action": "search"}),
        ("health_check", {}),
    ],
)
def test_reads_that_mint_nothing_stay_retry_safe(tool_name, arguments):
    call = resolve_call_operation(tool_name, arguments)
    assert call.mints_identity is False
    assert call.retry_safe is True


@pytest.mark.asyncio
async def test_router_level_timeout_classifies_the_routed_action():
    """knowledge(action='note') has no per-action timeout, so the router's fires."""
    with patch("src.coordination_failure_emit.emit_coordination_failure_sync"):
        wrapper = mcp_tool("knowledge", timeout=0.05, register=False)(_sleeper())
        write = _payload(await wrapper({"action": "note", "agent_id": "agent-1"}))
        wrapper = mcp_tool("knowledge", timeout=0.05, register=False)(_sleeper())
        read = _payload(await wrapper({"action": "search"}))

    assert write["outcome"] == "unknown"
    assert write["recovery"]["check_before_retry"] == STORE_CHECK
    assert read["recovery"]["action"] == READ_RECOVERY


@pytest.mark.asyncio
async def test_other_write_timeout_points_at_a_read_before_any_retry():
    @mcp_tool("process_agent_update", timeout=0.05, register=False)
    async def _slow(arguments):
        await asyncio.sleep(5)

    with patch("src.coordination_failure_emit.emit_coordination_failure_sync"):
        payload = _payload(await _slow({}))

    assert payload["outcome"] == "unknown"
    assert payload["operation"] == "write"
    recovery = payload["recovery"]
    assert recovery["check_before_retry"] == "describe_tool(tool_name='process_agent_update')"
    assert recovery["action"].startswith("Do not call process_agent_update again yet")
    assert "try again" not in recovery["action"].lower()


@pytest.mark.asyncio
async def test_unclassified_tool_timeout_does_not_invite_a_retry():
    @mcp_tool("plugin_tool_without_metadata", timeout=0.05, register=False)
    async def _slow(arguments):
        await asyncio.sleep(5)

    with patch("src.coordination_failure_emit.emit_coordination_failure_sync"):
        payload = _payload(await _slow({}))

    assert payload["outcome"] == "unknown"
    assert payload["operation"] is None
    assert READ_RECOVERY not in json.dumps(payload)


@pytest.mark.asyncio
async def test_a_failing_classifier_still_answers_with_an_unknown_outcome():
    @mcp_tool("update_discovery_status_graph", timeout=0.05, register=False)
    async def _slow(arguments):
        await asyncio.sleep(5)

    with patch("src.coordination_failure_emit.emit_coordination_failure_sync"), patch(
        "src.mcp_handlers.decorators._resolve_call_operation",
        side_effect=RuntimeError("metadata unavailable"),
    ):
        payload = _payload(await _slow({"discovery_id": DISCOVERY_ID}))

    assert payload["error_code"] == "TIMEOUT"
    assert payload["outcome"] == "unknown"
    assert READ_RECOVERY not in json.dumps(payload)


def _sleeper():
    async def _slow(arguments):
        await asyncio.sleep(5)

    return _slow


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        ("update_discovery_status_graph", {"action": "update"}, ("write", "knowledge", "update")),
        ("store_knowledge_graph", {"action": "store"}, ("write", "knowledge", "store")),
        ("supersede_discovery", {}, ("write", "knowledge", "supersede")),
        ("get_discovery_details", {}, ("read", "knowledge", "details")),
        ("search_knowledge_graph", {}, ("read", "search_knowledge_graph", None)),
        ("leave_note", {}, ("write", "leave_note", None)),
        ("knowledge", {"action": "note"}, ("write", "knowledge", "note")),
        ("knowledge", {"action": "update"}, ("write", "knowledge", "update")),
        ("knowledge", {"action": "details"}, ("read", "knowledge", "details")),
        ("knowledge", {"action": "search"}, ("read", "knowledge", "search")),
        ("knowledge", {"op": "search"}, ("read", "knowledge", "search")),
        # No class of its own: the router's, which errs toward write.
        ("knowledge", {"action": "audit"}, ("write", "knowledge", "audit")),
        ("observe", {"action": "bridge"}, ("read", "observe", "bridge")),
        ("agent", {"action": "list"}, ("read", "agent", "list")),
        ("agent", {"action": "delete"}, ("write", "agent", "delete")),
        ("process_agent_update", {}, ("write", "process_agent_update", None)),
        ("health_check", {}, ("read", "health_check", None)),
        ("not_a_registered_tool", {}, (None, "not_a_registered_tool", None)),
    ],
)
def test_resolve_call_operation(tool_name, arguments, expected):
    call = resolve_call_operation(tool_name, arguments)
    assert (call.operation, call.tool, call.action) == expected


def test_every_first_party_tool_and_router_action_has_an_operation_class():
    """A timeout reply depends on the class; a new tool must not fall to None.

    None is answered as an unknown outcome, which is safe, but a read that
    lands there loses its plain retry advice without anyone deciding so.
    """
    unclassified = []
    for name, definition in _TOOL_DEFINITIONS.items():
        if not _is_first_party_module(definition.source_module):
            continue
        if name in _ROUTER_ACTION_HANDLERS:
            for action, handler in _ROUTER_ACTION_HANDLERS[name].items():
                if resolve_call_operation(name, {"action": action}).operation is None:
                    unclassified.append(f"{name}(action='{action}')")
                inner = getattr(handler, "_mcp_tool_name", None)
                if inner and resolve_call_operation(inner, {"action": action}).operation is None:
                    unclassified.append(inner)
        elif resolve_call_operation(name, {}).operation is None:
            unclassified.append(name)
    assert unclassified == []


@pytest.mark.parametrize(
    "value",
    ["it's", "back\\slash", "line\nbreak", "", None, 42, "x" * 201],
)
def test_call_literal_falls_back_to_the_placeholder(value):
    assert _call_literal(value, "<discovery_id>") == "<discovery_id>"


def test_call_literal_keeps_an_ordinary_id():
    assert _call_literal(DISCOVERY_ID, "<discovery_id>") == DISCOVERY_ID
