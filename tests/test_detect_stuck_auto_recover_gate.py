"""detect_stuck_agents: the auto_recover branch is operator-only.

The sweep runs pre_onboard, so an unbound caller reaches the handler. Reading
the stuck set is fine; the auto_recover branch archives superseded parents,
opens dialectic sessions against other agents and resumes them, so it takes
an X-Unitares-Operator token like the other cross-agent lifecycle writes.
The in-process background recovery task keeps working through
run_stuck_agent_sweep.
"""

import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.context import (
    SessionSignals,
    reset_session_signals,
    set_session_signals,
)

_STUCK = "src.mcp_handlers.lifecycle.stuck"
_TOKEN = "test-operator-token-0123456789abcdef"
_STUCK_ENTRY = {
    "agent_id": "agent-1",
    "reason": "critical_margin_timeout",
    "age_minutes": 60.0,
    "details": "Critical margin",
}


def _payload(result):
    return json.loads(result[0].text)


@pytest.fixture
def sweep_mocks():
    """Stub detection and both mutating helpers; record what ran."""
    archive = AsyncMock(return_value=[{"agent_id": "p1", "action": "archived"}])
    recover = AsyncMock(return_value=[{"agent_id": "agent-1", "action": "auto_resumed"}])
    with patch(f"{_STUCK}.mcp_server") as server, \
         patch(f"{_STUCK}._detect_stuck_agents", return_value=[dict(_STUCK_ENTRY)]) as detect, \
         patch(f"{_STUCK}._load_agent_report_cadences", AsyncMock(return_value={})), \
         patch(f"{_STUCK}._archive_superseded_parents", archive), \
         patch(f"{_STUCK}._try_recover_agent", recover), \
         patch("src.audit_log.audit_logger._write_entry"):
        server.load_metadata_async = AsyncMock()
        yield {"server": server, "detect": detect, "archive": archive, "recover": recover}


@contextmanager
def _request_signals(**kwargs):
    """Install request SessionSignals in the test's own context."""
    token = set_session_signals(SessionSignals(**kwargs))
    try:
        yield
    finally:
        reset_session_signals(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, "true", "1", "YES", 1])
async def test_unbound_auto_recover_is_refused_without_mutation(sweep_mocks, value, monkeypatch):
    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", _TOKEN)
    from src.mcp_handlers.lifecycle.stuck import handle_detect_stuck_agents

    data = _payload(await handle_detect_stuck_agents({"auto_recover": value}))

    assert data["success"] is False
    assert data["error_code"] == "NOT_OPERATOR"
    sweep_mocks["archive"].assert_not_awaited()
    sweep_mocks["recover"].assert_not_awaited()
    sweep_mocks["server"].load_metadata_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_operator_token_is_refused(sweep_mocks, monkeypatch):
    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", _TOKEN)
    from src.mcp_handlers.lifecycle.stuck import handle_detect_stuck_agents

    with _request_signals(unitares_operator_token="not-the-token", transport="rest"):
        data = _payload(await handle_detect_stuck_agents({"auto_recover": True}))

    assert data["error_code"] == "NOT_OPERATOR"
    sweep_mocks["recover"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"auto_recover": False}, {"auto_recover": "false"}, {"auto_recover": "0"}])
async def test_unbound_detect_only_still_works(sweep_mocks, args, monkeypatch):
    """The read path stays pre_onboard; a string "false" is not a request."""
    monkeypatch.delenv("UNITARES_OPERATOR_TOKENS", raising=False)
    from src.mcp_handlers.lifecycle.stuck import handle_detect_stuck_agents

    data = _payload(await handle_detect_stuck_agents(args))

    assert data["success"] is True
    assert data["summary"]["total_stuck"] == 1
    assert data["recovered"] == []
    sweep_mocks["archive"].assert_not_awaited()
    sweep_mocks["recover"].assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_token_runs_recovery(sweep_mocks, monkeypatch):
    monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", _TOKEN)
    from src.mcp_handlers.lifecycle.stuck import handle_detect_stuck_agents

    with _request_signals(unitares_operator_token=_TOKEN, transport="rest"):
        data = _payload(await handle_detect_stuck_agents({"auto_recover": True}))

    assert data["success"] is True
    sweep_mocks["archive"].assert_awaited_once()
    sweep_mocks["recover"].assert_awaited_once()
    assert {r["action"] for r in data["recovered"]} == {"archived", "auto_resumed"}


@pytest.mark.asyncio
async def test_in_process_sweep_recovers_without_request_context(sweep_mocks, monkeypatch):
    """The background task has no request signals; it uses the internal entry."""
    monkeypatch.delenv("UNITARES_OPERATOR_TOKENS", raising=False)
    from src.mcp_handlers.lifecycle.stuck import run_stuck_agent_sweep

    data = _payload(await run_stuck_agent_sweep({}, auto_recover=True))

    assert data["success"] is True
    sweep_mocks["recover"].assert_awaited_once()


@pytest.mark.asyncio
async def test_background_task_calls_internal_sweep_with_recovery():
    import asyncio

    from src import background_tasks

    sweep = AsyncMock(return_value=[])
    sleeps = AsyncMock(side_effect=[None, None, asyncio.CancelledError()])
    original_wait_for = asyncio.wait_for
    with patch(f"{_STUCK}.run_stuck_agent_sweep", sweep), \
         patch.object(background_tasks.asyncio, "sleep", sleeps), \
         patch.object(background_tasks.asyncio, "wait_for", wraps=original_wait_for) as bounded:
        await background_tasks.stuck_agent_recovery_task()

    sweep.assert_awaited_once()
    args, kwargs = sweep.await_args
    assert kwargs == {"auto_recover": True}
    assert "auto_recover" not in args[0]
    assert bounded.await_args.kwargs["timeout"] == 15.0


@pytest.mark.asyncio
async def test_background_task_continues_after_sweep_timeout():
    import asyncio
    from src import background_tasks

    async def timed_out(awaitable, *, timeout):
        assert timeout == 15.0
        awaitable.close()
        raise asyncio.TimeoutError()

    sleeps = AsyncMock(side_effect=[None, None, None, asyncio.CancelledError()])
    with patch(f"{_STUCK}.run_stuck_agent_sweep", AsyncMock()), \
         patch.object(background_tasks.asyncio, "sleep", sleeps), \
         patch.object(background_tasks.asyncio, "wait_for", side_effect=timed_out) as bounded:
        await background_tasks.stuck_agent_recovery_task()
    bounded.assert_awaited_once()
    assert sleeps.await_count == 4
