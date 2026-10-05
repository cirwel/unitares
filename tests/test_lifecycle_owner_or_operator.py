"""archive, delete and resume accept only the agent itself or an operator.

GHSA-r9q5-7j8h-82rr: these three actions are reached through the consolidated
``agent`` tool, which ``inject_identity`` deliberately lets name another agent
as the target (it is in ``_OPERATOR_TARGET_CALLS``), and until this fix the
handlers did no check of their own, so any bound caller could archive, delete
or unpause any other agent. The handlers now call ``require_owner_or_operator``:
the session binding must resolve to the target, or the request must carry a
valid ``X-Unitares-Operator`` token. A bearer API key alone, an unbound
caller, and a self-claimed ``operator`` label or tag all stay refused.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.lifecycle.mutation import PRIVILEGED_TAGS
from tests.helpers import make_agent_meta, make_mock_server, patch_agent_storage, patch_lifecycle_server
from tests.lifecycle_auth import TEST_OPERATOR_TOKEN, bound_caller, no_operator, operator_caller

TARGET = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER = "bbbbbbbb-0000-4000-8000-000000000002"

REFUSAL = "LIFECYCLE_NOT_OWNER_OR_OPERATOR"


def _body(result) -> dict:
    return json.loads(result[0].text)


async def _archive(server):
    from src.mcp_handlers.lifecycle.mutation import handle_archive_agent

    return await handle_archive_agent({"agent_id": TARGET})


async def _delete(server):
    from src.mcp_handlers.lifecycle.mutation import handle_delete_agent

    return await handle_delete_agent({"agent_id": TARGET, "confirm": True, "backup_first": False})


async def _resume(server):
    from src.mcp_handlers.lifecycle.operations import handle_resume_agent

    return await handle_resume_agent({"agent_id": TARGET})


ACTIONS = {
    "archive": (_archive, "active", "archived successfully"),
    "delete": (_delete, "active", "deleted successfully"),
    "resume": (_resume, "paused", "resumed"),
}


@pytest.fixture
def server():
    return make_mock_server()


@pytest.fixture(params=sorted(ACTIONS), ids=sorted(ACTIONS))
def action(request, server):
    call, status, success_marker = ACTIONS[request.param]
    extra = {"paused_at": "2026-01-01T00:00:00+00:00"} if status == "paused" else {}
    meta = make_agent_meta(status=status, tags=[], **extra)
    server.agent_metadata = {TARGET: meta}
    with patch_lifecycle_server(server, require_registered=(TARGET, None)), \
         patch_agent_storage() as storage:
        storage.update_agent = AsyncMock(return_value=True)
        storage.persist_runtime_state = AsyncMock(return_value=True)
        storage.archive_agent = AsyncMock(return_value=True)
        storage.delete_agent = AsyncMock(return_value=True)
        yield request.param, call, meta, status, success_marker


def _assert_refused(body: dict, meta, status: str, caller: str) -> None:
    assert body.get("success") is False, body
    assert body.get("error_code") == REFUSAL, body
    # error_response flattens ``details`` into the body; accept either shape.
    details = {**(body.get("details") or {}), **body}
    assert details.get("error_type") == "ownership_violation", body
    assert details.get("owner_agent_id") == TARGET, body
    assert details.get("caller_agent_id") == caller, body
    assert meta.status == status, "the refusal must leave the target untouched"
    meta.add_lifecycle_event.assert_not_called()


@pytest.mark.asyncio
async def test_bound_non_owner_without_token_is_refused(action, server):
    name, call, meta, status, _ = action
    with bound_caller(OTHER), no_operator():
        body = _body(await call(server))
    _assert_refused(body, meta, status, OTHER)


@pytest.mark.asyncio
async def test_unbound_caller_is_refused(action, server):
    name, call, meta, status, _ = action
    with bound_caller(None), no_operator():
        body = _body(await call(server))
    _assert_refused(body, meta, status, "unbound")


@pytest.mark.asyncio
async def test_token_not_on_the_allowlist_is_refused(action, server, monkeypatch):
    """Presenting a token is not enough: it has to match UNITARES_OPERATOR_TOKENS."""
    name, call, meta, status, _ = action
    with bound_caller(OTHER), operator_caller("some-other-token"):
        monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", TEST_OPERATOR_TOKEN)
        body = _body(await call(server))
    _assert_refused(body, meta, status, OTHER)


@pytest.mark.asyncio
async def test_the_agent_itself_may_proceed(action, server):
    name, call, meta, status, success_marker = action
    with bound_caller(TARGET), no_operator():
        body = _body(await call(server))
    assert body.get("success") is True, body
    assert success_marker in json.dumps(body).lower()


@pytest.mark.asyncio
async def test_operator_token_may_target_another_agent(action, server):
    name, call, meta, status, success_marker = action
    with bound_caller(OTHER), operator_caller():
        body = _body(await call(server))
    assert body.get("success") is True, body
    assert success_marker in json.dumps(body).lower()


def test_operator_is_a_privileged_tag():
    """``operator`` can no longer be self-assigned through update_agent_metadata."""
    assert "operator" in PRIVILEGED_TAGS


@pytest.mark.asyncio
async def test_operator_resume_agent_ignores_label_and_tag(server):
    """operator_resume_agent keys on the token, never on caller-claimed metadata."""
    from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent

    caller = MagicMock(label="Operator", tags=["operator"])
    target = MagicMock(status="paused", paused_at=None)
    server.agent_metadata = {OTHER: caller, TARGET: target}
    monitor = MagicMock()
    monitor.state.coherence = 0.6
    monitor.state.void_active = False
    monitor.state.V = 0.0
    monitor.get_metrics.return_value = {"risk_score": 0.3, "risk_score_source": "resolved", "mean_risk": 0.3}
    server.get_or_create_monitor.return_value = monitor
    arguments = {"_agent_uuid": OTHER, "target_agent_id": TARGET, "reason": "unstick"}

    with patch("src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
               return_value=("caller", None)), \
         patch("src.mcp_handlers.lifecycle.self_recovery.mcp_server", server), \
         patch("src.mcp_handlers.lifecycle.self_recovery.store_discovery_internal",
               new_callable=AsyncMock, create=True), \
         patch("src.agent_storage.update_agent", new_callable=AsyncMock), \
         patch("src.agent_storage.persist_runtime_state", new_callable=AsyncMock):
        with bound_caller(OTHER), no_operator():
            refused = _body(await handle_operator_resume_agent(dict(arguments)))
        assert refused.get("success") is False
        assert refused.get("error_code") == "NOT_OPERATOR"

        with bound_caller(OTHER), operator_caller():
            allowed = _body(await handle_operator_resume_agent(dict(arguments)))
        assert allowed.get("data", allowed).get("success") is True, allowed
