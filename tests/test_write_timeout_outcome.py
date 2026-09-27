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
from datetime import datetime, timedelta, timezone
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


BOUND_UUID = "5b7c0e1a-2d3f-4a5b-8c6d-7e8f9a0b1c2d"


def _store_lookup_payload(arguments: dict, *, bound: str | None = None) -> dict:
    """A store/note timeout reply for ``arguments`` under a short timeout."""

    async def _run():
        @mcp_tool("store_knowledge_graph", timeout=0.05, register=False)
        async def _slow(args):
            await asyncio.sleep(5)

        return _payload(await _slow(dict(arguments)))

    with patch("src.coordination_failure_emit.emit_coordination_failure_sync"), patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=bound
    ):
        return asyncio.run(_run())


def _assert_window_lookup(payload: dict, writer: str | None) -> dict:
    """The check lists the writer's rows created in the call's own window."""
    from src.mcp_handlers.error_helpers import _render_call

    recovery = payload["recovery"]
    lookup = recovery["check_arguments"]
    expected = {"action": "search"}
    if writer is not None:
        expected["agent_id_filter"] = writer
    expected.update(
        {
            "created_after": payload["call_started_at"],
            "created_before": payload["settled_by"],
            "sort_by": "created_at",
            "include_archived": True,
            "include_cold": True,
            "limit": 100,
        }
    )
    assert lookup == expected
    assert "query" not in lookup, "a relevance query would rank and cut the page"
    assert recovery["check_before_retry"] == _render_call("knowledge", lookup)
    assert recovery["check_before_retry"] in recovery["action"]
    assert "not relevance" in recovery["action"]
    assert "count is below 100" in recovery["action"]
    assert "new row" in recovery["action"]
    assert "try again" not in recovery["action"].lower()
    return recovery


def test_bound_store_timeout_lists_the_identitys_rows_in_the_call_window():
    payload = _store_lookup_payload({"action": "store", "summary": "s"}, bound=BOUND_UUID)

    assert payload["outcome"] == "unknown"
    recovery = _assert_window_lookup(payload, BOUND_UUID)
    assert "Rows under your identity come from calls bound to it" in recovery["action"]
    assert "no ownership check stops" in recovery["action"]
    assert recovery["workflow"][1].endswith("it was saved. Do not store it again")


def test_anonymous_store_timeout_names_the_pseudonym_the_handler_writes():
    """The low-friction path records a pseudonym derived from the session; it is
    not a registered agent, so the lookup filters on it rather than asking
    knowledge(action='get', agent_id=...), which would refuse this caller."""
    from src.mcp_handlers.knowledge import handlers as kg_handlers

    arguments = {"action": "store", "summary": "s", "client_session_id": "sess-abc"}
    payload = _store_lookup_payload(arguments)

    pseudonym = kg_handlers._derive_anonymous_writer_id(dict(arguments))
    assert pseudonym.startswith("anonkg_") and not pseudonym.endswith("_local")
    recovery = _assert_window_lookup(payload, pseudonym)
    assert "derived from your session's signals" in recovery["action"]
    assert "only its absence is proof" in recovery["action"]
    assert "may be another caller's" in recovery["workflow"][1]


def test_a_shared_anonymous_pseudonym_is_not_claimed_as_proof_of_authorship():
    payload = _store_lookup_payload({"action": "store", "summary": "s"})

    writer = payload["recovery"]["check_arguments"]["agent_id_filter"]
    assert writer.startswith("anonkg_") and writer.endswith("_local")
    recovery = _assert_window_lookup(payload, writer)
    assert "carries no identifying signal" in recovery["action"]
    assert "only its absence is proof" in recovery["action"]


@pytest.mark.parametrize(
    "writer",
    [
        "agent-1",
        # knowledge(action='note') runs its handler on the router's own dict,
        # so a timed-out note can already carry the pseudonym it resolved.
        "anonkg_mcp_0123456789ab",
    ],
)
def test_a_named_writer_is_filtered_on_but_not_claimed_as_the_callers(writer):
    payload = _store_lookup_payload({"action": "store", "agent_id": writer})

    recovery = _assert_window_lookup(payload, writer)
    assert "That id is not yours alone" in recovery["action"]


def test_an_unresolvable_writer_lists_every_writer_and_says_so():
    """A high-severity store needs a registered agent; with none the handler
    would refuse, so no author can be named and the window is not filtered."""
    server = MagicMock()
    server.agent_metadata = {}
    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=server):
        payload = _store_lookup_payload({"action": "store", "severity": "high"})

    recovery = _assert_window_lookup(payload, None)
    assert "could not be resolved" in recovery["action"]
    assert "may be another writer's" in recovery["action"]


def test_a_failing_author_resolution_is_logged_and_the_window_stays(caplog):
    with patch(
        "src.mcp_handlers.knowledge.handlers._resolve_low_friction_writer",
        side_effect=RuntimeError("context unavailable"),
    ), caplog.at_level("WARNING"):
        payload = _store_lookup_payload({"action": "store", "summary": "s"})

    _assert_window_lookup(payload, None)
    assert any(
        "author of a timed-out knowledge write" in record.getMessage()
        and record.levelname == "WARNING"
        for record in caplog.records
    )


def test_the_lookup_arguments_survive_the_knowledge_schema():
    """Every lookup field is declared for action=search: the unified schema
    drops undeclared fields, and a dropped include_cold would hide rows."""
    from src.mcp_handlers.schemas.knowledge import KnowledgeParams

    payload = _store_lookup_payload({"action": "store"}, bound=BOUND_UUID)
    lookup = payload["recovery"]["check_arguments"]

    dumped = KnowledgeParams.model_validate(lookup).model_dump(exclude_none=True)
    for key, value in lookup.items():
        assert dumped[key] == value, key
        if key != "action":
            assert key in KnowledgeParams.ACTION_FIELDS["search"], key


def _batch(size: int) -> list[dict]:
    return [
        {"discovery_type": "note", "summary": f"item {index}"} for index in range(size)
    ]


def test_a_single_store_timeout_speaks_of_one_row():
    payload = _store_lookup_payload({"action": "store", "summary": "s"}, bound=BOUND_UUID)

    recovery = _assert_window_lookup(payload, BOUND_UUID)
    assert "batch" not in json.dumps(recovery)
    assert recovery["action"].startswith("Do not store this again yet")
    assert recovery["workflow"][2].endswith("nothing was saved: store it again")


@pytest.mark.parametrize(
    ("bound", "shared"),
    [(BOUND_UUID, False), (None, True)],
    ids=["bound", "anonymous"],
)
def test_a_batch_store_timeout_resends_only_the_items_without_a_row(bound, shared):
    """A batch commits item by item, so a timed-out batch can be half saved.
    Resending it whole stores every item that landed twice; the recovery lists
    the same window and has the caller resend only the unmatched items."""
    payload = _store_lookup_payload(
        {"action": "store", "discoveries": _batch(3)}, bound=bound
    )

    writer = payload["recovery"]["check_arguments"].get("agent_id_filter")
    recovery = _assert_window_lookup(payload, bound or writer)
    action = recovery["action"]
    assert action.startswith("Do not send this batch again. It was a batch of 3 items")
    assert "commits each item on its own" in action
    assert "resending the whole batch leaves a second finding" in action
    assert "send only the items with no row again" in action
    match_step, resend_step = recovery["workflow"][1], recovery["workflow"][2]
    assert match_step.startswith("2. Match each item you sent to its own row")
    assert "two items with the same summary need two rows" in match_step
    assert ("may be another caller's" in match_step) is shared
    assert "after settled_by" in resend_step
    assert "send only those items again" in resend_step
    assert resend_step.endswith("Never resend the whole batch")
    text = json.dumps(recovery)
    assert "store it again" not in text and "store this again" not in text, (
        "one-row wording would have the caller resend the whole batch"
    )


def test_a_batch_whose_size_the_arguments_do_not_give_is_still_a_batch():
    payload = _store_lookup_payload(
        {"action": "store", "discoveries": "not a list"}, bound=BOUND_UUID
    )

    action = _assert_window_lookup(payload, BOUND_UUID)["action"]
    assert action.startswith("Do not send this batch again. It was a batch, and")


@pytest.mark.parametrize(
    ("tool", "action", "arguments", "expected"),
    [
        ("knowledge", "store", {"discoveries": _batch(2)}, 2),
        ("knowledge", "store", {"discoveries": []}, 0),
        ("knowledge", "store", {"summary": "s"}, None),
        ("knowledge", "store", {"discoveries": None}, None),
        # Only the store handler takes a batch.
        ("knowledge", "note", {"discoveries": _batch(2)}, None),
        ("leave_note", None, {"discoveries": _batch(2)}, None),
    ],
)
def test_store_batch_size_follows_the_handlers_batch_test(tool, action, arguments, expected):
    from src.mcp_handlers.decorators import CallOperation
    from src.mcp_handlers.error_helpers import _store_batch_size

    call = CallOperation(operation="write", tool=tool, action=action)
    assert _store_batch_size(call, arguments) == expected


@pytest.mark.asyncio
async def test_a_batch_that_times_out_between_items_is_half_saved():
    """The real batch handler body: item 0 commits, item 1 outlasts the
    timeout. The reply must not have the caller resend item 0."""
    from src.mcp_handlers.knowledge import handlers as kg_handlers

    body = kg_handlers.handle_store_knowledge_graph.__wrapped__

    async def _store(arguments):
        return await body(arguments)

    handler = mcp_tool("store_knowledge_graph", timeout=0.2, register=False)(_store)
    saved: list[str] = []

    class _BatchGraph:
        async def find_similar(self, discovery, limit=3):
            return []

        async def add_discovery(self, discovery):
            if saved:
                await asyncio.sleep(5)
            saved.append(discovery.summary)

    graph = _BatchGraph()
    with patch(
        "src.mcp_handlers.utils.check_agent_can_operate", return_value=None
    ), patch(
        "src.mcp_handlers.knowledge.handlers._broadcast_knowledge_write",
        new_callable=AsyncMock,
    ):
        payload = _payload(
            await _run_patched(
                handler, graph, {"action": "store", "discoveries": _batch(2)}
            )
        )

    assert saved == ["item 0"], "premise: the first item committed, the second did not"
    assert payload["outcome"] == "unknown"
    recovery = payload["recovery"]
    assert "a batch of 2 items" in recovery["action"]
    assert recovery["workflow"][2].endswith("Never resend the whole batch")


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


def _identity_recovery(tool: str, arguments: dict) -> dict:
    from src.mcp_handlers.decorators import CallOperation
    from src.mcp_handlers.error_helpers import _unknown_outcome_recovery

    return _unknown_outcome_recovery(
        CallOperation(operation="read", tool=tool, mints_identity=True),
        arguments,
    )


def _is_binding_read(recovery: dict) -> bool:
    return recovery["check_before_retry"] == "identity(client_session_id='sess-1')"


def _is_onboard_minting(recovery: dict) -> bool:
    return (
        recovery["check_before_retry"] is None
        and "every further call creates another" in recovery["action"]
    )


@pytest.mark.parametrize(
    ("extra", "mints"),
    [
        # onboard reads resume with coerce_bool(default=True): false skips the
        # resume and mints a new identity beside the named binding.
        ({"resume": False}, True),
        ({"resume": "false"}, True),
        ({"resume": "0"}, True),
        ({}, False),
        ({"resume": True}, False),
        ({"resume": "true"}, False),
        ({"force_new": True}, True),
        ({"force_new": "true"}, True),
        ({"force_new": "false"}, False),
        ({"force_new": True, "resume": True}, True),
    ],
)
def test_an_onboard_that_asked_for_a_new_identity_is_not_sent_to_the_old_binding(
    extra, mints
):
    """A read of the named binding would find the old agent_uuid and call the
    fork settled whether or not it happened."""
    recovery = _identity_recovery("onboard", {"client_session_id": "sess-1", **extra})

    assert _is_onboard_minting(recovery) is mints
    assert _is_binding_read(recovery) is not mints


@pytest.mark.parametrize(
    ("raw", "mints"),
    [
        ({"client_session_id": "sess-1", "resume": "false"}, True),
        ({"client_session_id": "sess-1", "resume": False}, True),
        ({"client_session_id": "sess-1"}, False),
    ],
)
def test_onboard_classification_holds_on_the_validated_arguments(raw, mints):
    """Dispatch validates before the handler, so a timed-out call carries the
    schema's output: resume filled with true when omitted, strings coerced."""
    from src.mcp_handlers.schemas.identity import OnboardParams

    validated = OnboardParams.model_validate(raw).model_dump()
    assert _is_onboard_minting(_identity_recovery("onboard", validated)) is mints


@pytest.mark.parametrize("extra", [{}, {"resume": False}, {"resume": "false"}])
def test_identity_resume_false_is_the_ordinary_read_of_the_binding(extra):
    """identity reuses the binding dispatch resolved whatever resume says, and
    its schema fills an omitted resume with false: resume=false there is the
    ordinary read, not a request for a new identity."""
    from src.mcp_handlers.schemas.identity import IdentityParams

    arguments = {"client_session_id": "sess-1", **extra}
    validated = IdentityParams.model_validate(arguments).model_dump()
    assert validated["resume"] is False, "premise: the schema's default"

    for sent in (arguments, validated):
        assert _is_binding_read(_identity_recovery("identity", sent))


def test_identity_force_new_takes_the_identity_minting_recovery():
    recovery = _identity_recovery(
        "identity", {"client_session_id": "sess-1", "force_new": True}
    )

    assert not _is_binding_read(recovery)
    assert "identity creates one when no binding is proven" in recovery["action"]


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
    assert write["recovery"]["check_arguments"]["agent_id_filter"] == "agent-1"
    assert write["recovery"]["check_arguments"]["sort_by"] == "created_at"
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


# ---------------------------------------------------------------------------
# The store lookup is exhaustive for the window, on both backends
# ---------------------------------------------------------------------------
#
# Codex review on #2543: the store recovery used to name a relevance search
# for the summary. That page is ranked and bounded (default 20), so the saved
# row could rank off it and absence read as "nothing saved", and another
# writer's row with the same summary matched too. The lookup is now a
# queryless search filtered to the writer and a created_at window. These run
# the recovery's own check_arguments through the real search handler against
# governance_test (the live_postgres_backend fixture; skipped without it).

PROBE = "timeout lookup probe"
OTHER_WRITER = "0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
ANON_WRITER = "anonkg_mcp_0123456789ab"


def _row(suffix: str, *, writer: str, created: datetime, summary: str, status="open",
         details: str = "") -> DiscoveryNode:
    return DiscoveryNode(
        id=f"{created.isoformat()}-{suffix}",
        agent_id=writer,
        type="note",
        summary=summary,
        details=details,
        tags=["timeout-lookup"],
        severity="low",
        status=status,
        timestamp=created.isoformat(),
    )


def _window():
    started = datetime.now(timezone.utc) - timedelta(seconds=20)
    return started, started + timedelta(seconds=45)


def _pg_graph(db):
    from src.storage.knowledge_graph_postgres import KnowledgeGraphPostgres

    graph = KnowledgeGraphPostgres()
    graph._get_db = AsyncMock(return_value=db)
    return graph


def _age_graph(db):
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE

    graph = KnowledgeGraphAGE()
    graph._get_db = AsyncMock(return_value=db)
    return graph


GRAPHS = {"postgres": _pg_graph, "age": _age_graph}


async def _search(graph, arguments: dict) -> dict:
    from src.mcp_handlers.knowledge import handlers as kg_handlers

    with patch.object(kg_handlers, "get_knowledge_graph", AsyncMock(return_value=graph)), \
         patch.object(kg_handlers, "_broadcast_knowledge_read", AsyncMock()), \
         patch.object(
             kg_handlers,
             "_resolve_agent_display",
             MagicMock(side_effect=lambda agent_id: {"display_name": agent_id}),
         ):
        result = await kg_handlers.handle_search_knowledge_graph(dict(arguments))
    return json.loads(result[0].text)


def _lookup_for(writer: str, started: datetime, settled: datetime) -> dict:
    """The recovery's check_arguments for a store timed out by ``writer``."""
    from src.mcp_handlers.decorators import CallOperation
    from src.mcp_handlers.error_helpers import _unknown_outcome_recovery

    bound = writer if not writer.startswith("anonkg_") else None
    arguments = {} if bound else {"agent_id": writer}
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=bound):
        recovery = _unknown_outcome_recovery(
            CallOperation(operation="write", tool="knowledge", action="store"),
            arguments,
            call_started_at=started.isoformat(),
            settled_by=settled.isoformat(),
        )
    lookup = recovery["check_arguments"]
    assert lookup["agent_id_filter"] == writer
    return lookup


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(GRAPHS))
@pytest.mark.parametrize("writer", [BOUND_UUID, ANON_WRITER])
async def test_the_store_lookup_lists_exactly_this_writers_rows_in_the_window(
    live_postgres_backend, backend, writer
):
    started, settled = _window()
    ours = [
        _row("open", writer=writer, created=started + timedelta(seconds=1),
             summary=PROBE, details="unrelated narrative " * 40),
        _row("archived", writer=writer, created=started + timedelta(seconds=2),
             summary="archived at store time", status="archived"),
        _row("cold", writer=writer, created=started + timedelta(seconds=3),
             summary="cold at store time", status="cold"),
    ]
    excluded = [
        # This writer, before the call began.
        _row("before", writer=writer, created=started - timedelta(seconds=60),
             summary=PROBE),
        # This writer, after settled_by.
        _row("after", writer=writer, created=settled + timedelta(seconds=5),
             summary=PROBE),
        # Another writer, same summary, inside the window.
        _row("other", writer=OTHER_WRITER, created=started + timedelta(seconds=1),
             summary=PROBE),
    ]
    # Rows that outrank ours on relevance for the summary's words: 30 of them,
    # more than the default page of 20.
    noise = [
        _row(f"noise-{i:02d}", writer=OTHER_WRITER,
             created=started + timedelta(seconds=4, milliseconds=i),
             summary=f"{PROBE}: {PROBE}, {PROBE}", details=f"{PROBE} " * 20)
        for i in range(30)
    ]
    for node in ours + excluded + noise:
        await live_postgres_backend.kg_add_discovery(node)
    graph = GRAPHS[backend](live_postgres_backend)

    if backend == "postgres":
        # The premise: the relevance search the recovery used to name loses
        # the row off its page, while another writer's same summary matches.
        relevance = await _search(graph, {"action": "search", "query": PROBE})
        relevance_ids = [d["id"] for d in relevance["discoveries"]]
        assert ours[0].id not in relevance_ids
        assert any(d["_agent_id"] == OTHER_WRITER for d in relevance["discoveries"])

    lookup = _lookup_for(writer, started, settled)
    payload = await _search(graph, lookup)

    assert payload["success"] is True, payload
    assert [d["id"] for d in payload["discoveries"]] == [n.id for n in reversed(ours)]
    assert {d["_agent_id"] for d in payload["discoveries"]} == {writer}
    assert payload["count"] == 3 < lookup["limit"]
    assert "_more_available" not in payload
    assert "limit_clamped_from" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", sorted(GRAPHS))
async def test_a_full_page_is_flagged_and_paging_back_reaches_the_rest(live_postgres_backend, backend):
    """Step 4: at count == limit the page may be cut; created_before set to
    the oldest row listed reads the older rest of the window."""
    started, settled = _window()
    rows = [
        _row(f"r{i:03d}", writer=BOUND_UUID,
             created=started + timedelta(milliseconds=100 * (i + 1)), summary=f"row {i}")
        for i in range(101)
    ]
    for node in rows:
        await live_postgres_backend.kg_add_discovery(node)
    graph = GRAPHS[backend](live_postgres_backend)

    lookup = _lookup_for(BOUND_UUID, started, settled)
    first = await _search(graph, lookup)
    assert first["count"] == 100
    assert "_more_available" in first
    assert rows[0].id not in {d["id"] for d in first["discoveries"]}, (
        "premise: the oldest row, nearest the call's start, is the one cut"
    )

    rest = await _search(
        graph, {**lookup, "created_before": first["discoveries"][-1]["created_at"]}
    )
    assert [d["id"] for d in rest["discoveries"]] == [rows[0].id]
    assert rest["count"] == 1 < lookup["limit"]


@pytest.mark.asyncio
async def test_a_failed_windowed_read_on_age_is_an_error_not_an_empty_window():
    """An empty window is the lookup's proof that nothing was saved, so the AGE
    backend must not turn a failed read into one. The postgres backend raises
    here already."""
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE

    db = MagicMock()
    db.kg_query = AsyncMock(side_effect=RuntimeError("connection reset"))
    graph = KnowledgeGraphAGE()
    graph._get_db = AsyncMock(return_value=db)
    started, settled = _window()

    with pytest.raises(RuntimeError, match="connection reset"):
        await graph.query(agent_id=BOUND_UUID, created_after=started, limit=100)

    payload = await _search(graph, _lookup_for(BOUND_UUID, started, settled))
    assert payload["success"] is False
    assert "connection reset" in payload["error"]

    # An unwindowed tag read keeps its old failure shape.
    assert await graph.query(tags=["timeout-lookup"], limit=5) == []


def test_an_alias_lookup_failure_is_logged_and_leaves_the_call_unclassified(
    monkeypatch, caplog
):
    """Watcher P006 on this PR: the ImportError fallback in _handler_operation
    swallowed the failure without a log. It now reaches resolve_call_operation,
    which logs it and answers with an unclassified call, never a retry-safe one."""
    import sys

    monkeypatch.setitem(sys.modules, "src.mcp_handlers.tool_stability", None)
    with caplog.at_level("WARNING"):
        call = resolve_call_operation("store_knowledge_graph", {"action": "store"})

    assert call.operation is None
    assert call.retry_safe is False
    assert any(
        record.levelname == "WARNING"
        and "resolve_call_operation failed" in record.getMessage()
        for record in caplog.records
    )


# --- Review round on the store lookup: what the author resolution must match --


def _label_owner_server(label="Label-Y", uuid="0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"):
    from types import SimpleNamespace

    server = MagicMock()
    server.agent_metadata = {
        uuid: SimpleNamespace(
            label=label, public_agent_id=None, structured_id=None,
            display_name=label, status="active",
        )
    }
    return server, uuid


def test_a_high_severity_note_keeps_the_writer_the_note_handler_records():
    """A note always takes the low-friction writer, whatever severity it
    carries: an explicit agent_id stays as sent. Resolving it through the
    store's registered-agent policy would filter on a different agent and hide
    the row the note wrote."""
    from src.mcp_handlers.knowledge import handlers as kg_handlers

    server, uuid = _label_owner_server()
    arguments = {"action": "note", "summary": "s", "severity": "high", "agent_id": "Label-Y"}
    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=server), \
         patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
        written, error, _ = kg_handlers._resolve_low_friction_writer(dict(arguments))
        author = kg_handlers.resolve_knowledge_write_author(arguments, store=False)

    assert error is None and written == "Label-Y"
    assert author is not None and author.agent_id == written
    assert author.agent_id != uuid


def test_author_resolution_leaves_the_callers_arguments_as_sent():
    """The low-friction writer policy writes the pseudonym it picks into the
    arguments it is given; the recovery must run it on a copy."""
    from src.mcp_handlers.knowledge import handlers as kg_handlers

    arguments = {"action": "store", "summary": "s", "client_session_id": "sess-abc"}
    sent = dict(arguments)
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
        author = kg_handlers.resolve_knowledge_write_author(arguments, store=True)

    assert author is not None and author.agent_id.startswith("anonkg_")
    assert arguments == sent


@pytest.mark.asyncio
@pytest.mark.parametrize("search_mode", ["semantic", "hybrid"])
async def test_a_windowed_semantic_read_failure_degrades_instead_of_failing_the_search(search_mode):
    """A windowed AGE query() raises on a failed SQL read so the timeout check
    cannot read an error as an empty window. Semantic search's in-memory
    fallback only ranks candidates, so it degrades to none; the search still
    succeeds on its full-text results."""
    import json
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from src.knowledge_graph import DiscoveryNode
    from src.mcp_handlers.knowledge import handlers as kg_handlers
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE

    db = MagicMock()
    db.kg_query = AsyncMock(side_effect=RuntimeError("connection reset"))
    db.graph_available = AsyncMock(return_value=True)
    graph = KnowledgeGraphAGE()
    graph._get_db = AsyncMock(return_value=db)
    graph._pgvector_available = AsyncMock(return_value=False)
    node = DiscoveryNode(
        id="2026-09-27T00:00:00+00:00", agent_id="a", type="note",
        summary="coherence gate note", details="", tags=[], severity="low",
    )
    graph.full_text_search = AsyncMock(return_value=[node])
    emb = MagicMock()
    emb.embed = AsyncMock(return_value=[0.1, 0.2])
    after = datetime.now(timezone.utc) - timedelta(days=30)

    with patch("src.embeddings.embeddings_available", return_value=True), \
         patch("src.embeddings.get_embeddings_service", AsyncMock(return_value=emb)):
        assert await graph.semantic_search("coherence gate", created_after=after) == []
        with patch.object(kg_handlers, "get_knowledge_graph", AsyncMock(return_value=graph)), \
             patch.object(kg_handlers, "_broadcast_knowledge_read", AsyncMock()), \
             patch.object(kg_handlers, "_resolve_agent_display",
                          MagicMock(side_effect=lambda a: {"display_name": a})):
            result = await kg_handlers.handle_search_knowledge_graph({
                "action": "search", "query": "coherence gate",
                "search_mode": search_mode, "created_after": after.isoformat(),
            })

    payload = json.loads(result[0].text)
    assert payload.get("success") is True, payload.get("error")
    assert payload.get("count") == 1
