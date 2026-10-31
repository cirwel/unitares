"""Fleet-wide archival sweeps require an operator to mutate or see agent ids.

``archive_old_test_agents`` and ``archive_orphan_agents`` iterate every
registered agent. With ``UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL`` on and
``dry_run=false`` they archive other agents, which single-target
``archive_agent`` allows only to the agent itself or an operator
(GHSA-r9q5-7j8h-82rr). A sweep has no single target, so only an operator
(valid ``X-Unitares-Operator`` token) may run it for real. Non-operators may
still preview, but get counts only, not other agents' ids.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.lifecycle_auth import bound_caller, no_operator, operator_caller

CALLER = "cccccccc-0000-4000-8000-00000000000c"
VICTIM = "aaaaaaaa-0000-4000-8000-000000000001"


def _payload(result):
    item = result[0] if isinstance(result, (list, tuple)) else result
    return json.loads(item.text)


def _server():
    meta = SimpleNamespace(
        agent_id=VICTIM,
        status="active",
        label="real-agent",
        display_name=None,
        total_updates=5,
        last_update=None,
        created_at=None,
    )
    server = MagicMock()
    server.agent_metadata = {VICTIM: meta}
    server.load_metadata_async = AsyncMock()
    server.monitors = {}
    return server


async def _old_test_sweep(arguments):
    with patch("src.mcp_handlers.lifecycle.operations.mcp_server", _server()), \
         patch("src.agent_lifecycle._agent_age_hours", return_value=1000.0), \
         patch(
             "src.mcp_handlers.lifecycle.operations._archive_one_agent",
             new=AsyncMock(return_value=True),
         ) as mock_arch:
        from src.mcp_handlers.lifecycle.operations import handle_archive_old_test_agents
        result = await handle_archive_old_test_agents(arguments)
    return _payload(result), mock_arch


def _orphan_engine(archived: bool):
    return AsyncMock(return_value=[
        {"id": VICTIM, "reason": "unlabeled_stale", "updates": 1, "archived": archived},
    ])


async def _orphan_sweep(arguments, archived=False):
    engine = _orphan_engine(archived)
    with patch("src.mcp_handlers.lifecycle.operations.mcp_server", _server()), \
         patch("src.agent_lifecycle.auto_archive_orphan_agents", new=engine):
        from src.mcp_handlers.lifecycle.operations import handle_archive_orphan_agents
        result = await handle_archive_orphan_agents(arguments)
    return _payload(result), engine


class TestArchiveOldTestAgentsGate:
    @pytest.mark.asyncio
    async def test_non_operator_mutation_refused(self, monkeypatch):
        monkeypatch.setenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", "true")
        with no_operator(), bound_caller(CALLER):
            body, mock_arch = await _old_test_sweep({"include_all": True, "dry_run": False})
        assert body["success"] is False
        assert body["error_code"] == "ARCHIVAL_SWEEP_OPERATOR_REQUIRED"
        mock_arch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrong_operator_token_refused(self, monkeypatch):
        monkeypatch.setenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", "true")
        monkeypatch.setenv("UNITARES_OPERATOR_TOKENS", "the-real-token")
        from src.mcp_handlers.context import (
            SessionSignals, reset_session_signals, set_session_signals,
        )
        tok = set_session_signals(
            SessionSignals(transport="rest", unitares_operator_token="guessed")
        )
        try:
            body, mock_arch = await _old_test_sweep({"include_all": True, "dry_run": False})
        finally:
            reset_session_signals(tok)
        assert body["error_code"] == "ARCHIVAL_SWEEP_OPERATOR_REQUIRED"
        mock_arch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_operator_mutation_proceeds(self, monkeypatch):
        monkeypatch.setenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", "true")
        with operator_caller():
            body, mock_arch = await _old_test_sweep({"include_all": True, "dry_run": False})
        assert body["archived_count"] == 1
        assert body["archived_agents"][0]["id"] == VICTIM
        assert "agent_ids_redacted" not in body
        mock_arch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_operator_preview_returns_counts_only(self, monkeypatch):
        monkeypatch.setenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", "true")
        with no_operator(), bound_caller(CALLER):
            body, mock_arch = await _old_test_sweep({"include_all": True})
        assert body["success"] is True
        assert body["would_archive_count"] == 1
        assert body["would_archive_agents"] == []
        assert body["archived_agents"] == []
        assert body["agent_ids_redacted"] is True
        assert VICTIM not in json.dumps(body)
        mock_arch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_operator_dry_run_false_without_env_is_preview(self, monkeypatch):
        # Auto-archival off: dry_run=false cannot mutate, so it is not refused,
        # just previewed (and redacted) as before.
        monkeypatch.delenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", raising=False)
        with no_operator(), bound_caller(CALLER):
            body, mock_arch = await _old_test_sweep({"include_all": True, "dry_run": False})
        assert body["success"] is True
        assert body["archived_count"] == 0
        assert body["agent_ids_redacted"] is True
        mock_arch.assert_not_awaited()


class TestArchiveOrphanAgentsGate:
    @pytest.mark.asyncio
    async def test_non_operator_mutation_refused_before_engine(self, monkeypatch):
        monkeypatch.setenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", "true")
        with no_operator(), bound_caller(CALLER):
            body, engine = await _orphan_sweep({"dry_run": False}, archived=True)
        assert body["success"] is False
        assert body["error_code"] == "ARCHIVAL_SWEEP_OPERATOR_REQUIRED"
        engine.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_operator_mutation_proceeds(self, monkeypatch):
        monkeypatch.setenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", "true")
        with operator_caller():
            body, engine = await _orphan_sweep({"dry_run": False}, archived=True)
        assert body["archived_count"] == 1
        assert len(body["archived_agents"]) == 1
        assert engine.await_args.kwargs["dry_run"] is False

    @pytest.mark.asyncio
    async def test_non_operator_preview_returns_counts_only(self, monkeypatch):
        monkeypatch.setenv("UNITARES_ENABLE_AUTO_AGENT_ARCHIVAL", "true")
        with no_operator(), bound_caller(CALLER):
            body, engine = await _orphan_sweep({})
        assert body["success"] is True
        assert body["would_archive_count"] == 1
        assert body["would_archive_agents"] == []
        assert body["agent_ids_redacted"] is True
        assert VICTIM[:12] not in json.dumps(body)
        assert engine.await_args.kwargs["dry_run"] is True
