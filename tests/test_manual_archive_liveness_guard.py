"""Liveness guard on the manual archive path + preview-first stale sweep.

Two complementary footgun fixes (2026-06-14 council-agent incident, where a
bulk manual archive swept agents that a workflow still expected):

1. ``handle_archive_agent`` refuses to silently archive an agent that looks
   live — a running process binding, recent activity, or a declared *causal*
   lineage edge — unless the caller passes ``force=true``.
2. ``handle_archive_old_test_agents`` defaults to ``dry_run=true`` (preview
   first), mirroring ``archive_orphan_agents``, so the fleet-wide
   ``include_all`` lever can't execute without an explicit opt-in.

These are complementary to PRs #720/#721, which harden the *automatic*
archival paths; this covers the *manual* path and the bulk-sweep default.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _payload(result):
    """Unwrap a handler's TextContent list into the JSON dict."""
    item = result[0] if isinstance(result, (list, tuple)) else result
    return json.loads(item.text)


def _make_meta(**overrides):
    meta = SimpleNamespace(
        agent_id="agent-uuid",
        status="active",
        archived_at=None,
        notes="",
        total_updates=5,
        last_update=None,
        parent_agent_id=None,
        spawn_reason=None,
    )
    for k, v in overrides.items():
        setattr(meta, k, v)
    meta.add_lifecycle_event = MagicMock()
    return meta


def _archive_patches(meta, agent_uuid="agent-uuid"):
    """Common patch set for handle_archive_agent tests."""
    mock_server = MagicMock()
    mock_server.agent_metadata = {agent_uuid: meta}
    mock_server.load_metadata_async = AsyncMock()
    mock_server.monitors = {}
    mock_storage = MagicMock(update_agent=AsyncMock(return_value=True))
    return mock_server, mock_storage


# ----------------------------------------------------------------------
# manual_archive_liveness_signals helper
# ----------------------------------------------------------------------


class TestLivenessSignalsHelper:
    @pytest.mark.asyncio
    async def test_no_signals_for_idle_unlineaged_agent(self):
        from src.mcp_handlers.lifecycle.helpers import manual_archive_liveness_signals

        meta = _make_meta()
        with patch(
            "src.mcp_handlers.identity.process_binding.get_live_bindings",
            new=AsyncMock(return_value=[]),
        ):
            signals = await manual_archive_liveness_signals("agent-uuid", meta)
        assert signals == []

    @pytest.mark.asyncio
    async def test_live_binding_is_a_signal(self):
        from src.mcp_handlers.lifecycle.helpers import manual_archive_liveness_signals

        meta = _make_meta()
        with patch(
            "src.mcp_handlers.identity.process_binding.get_live_bindings",
            new=AsyncMock(return_value=[{"pid": 123}]),
        ):
            signals = await manual_archive_liveness_signals("agent-uuid", meta)
        assert any("binding" in s for s in signals)

    @pytest.mark.asyncio
    async def test_causal_lineage_is_a_signal_but_new_session_is_not(self):
        from src.mcp_handlers.lifecycle.helpers import manual_archive_liveness_signals

        with patch(
            "src.mcp_handlers.identity.process_binding.get_live_bindings",
            new=AsyncMock(return_value=[]),
        ):
            causal = await manual_archive_liveness_signals(
                "u", _make_meta(parent_agent_id="parent", spawn_reason="subagent")
            )
            noisy = await manual_archive_liveness_signals(
                "u", _make_meta(parent_agent_id="parent", spawn_reason="new_session")
            )
        assert any("lineage" in s for s in causal)
        assert noisy == []  # coincidental new_session edge is not a keep-alive signal

    @pytest.mark.asyncio
    async def test_recent_activity_alone_is_not_a_signal(self):
        """A recent last_update is intentionally NOT a keep-alive signal — only
        a live process binding or causal lineage blocks archival."""
        from datetime import datetime, timezone
        from src.mcp_handlers.lifecycle.helpers import manual_archive_liveness_signals

        recent = datetime.now(timezone.utc).isoformat()
        with patch(
            "src.mcp_handlers.identity.process_binding.get_live_bindings",
            new=AsyncMock(return_value=[]),
        ):
            signals = await manual_archive_liveness_signals(
                "u", _make_meta(last_update=recent)
            )
        assert signals == []

    @pytest.mark.asyncio
    async def test_binding_lookup_failure_is_fail_open(self):
        from src.mcp_handlers.lifecycle.helpers import manual_archive_liveness_signals

        meta = _make_meta()
        with patch(
            "src.mcp_handlers.identity.process_binding.get_live_bindings",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            signals = await manual_archive_liveness_signals("u", meta)
        assert signals == []  # error omitted, not raised


# ----------------------------------------------------------------------
# handle_archive_agent guard
# ----------------------------------------------------------------------


class TestManualArchiveGuard:
    async def _run_archive(self, meta, arguments):
        agent_uuid = "agent-uuid"
        mock_server, mock_storage = _archive_patches(meta, agent_uuid)
        with patch("src.mcp_handlers.lifecycle.mutation.mcp_server", mock_server), \
             patch("src.mcp_handlers.lifecycle.mutation.agent_storage", mock_storage), \
             patch(
                 "src.mcp_handlers.lifecycle.mutation.require_registered_agent",
                 return_value=(agent_uuid, None),
             ), \
             patch(
                 "src.mcp_handlers.lifecycle.mutation.resolve_agent_uuid",
                 return_value=agent_uuid,
             ), \
             patch(
                 "src.mcp_handlers.lifecycle.helpers._archive_one_agent",
                 new=AsyncMock(return_value=True),
             ) as mock_arch, \
             patch(
                 "src.mcp_handlers.lifecycle.mutation._invalidate_agent_cache",
                 new=AsyncMock(),
             ), \
             patch(
                 "src.mcp_handlers.identity.process_binding.get_live_bindings",
                 new=AsyncMock(return_value=meta._live_bindings),
             ):
            from src.mcp_handlers.lifecycle.mutation import handle_archive_agent
            result = await handle_archive_agent(arguments)
        return result, mock_arch

    @pytest.mark.asyncio
    async def test_refuses_archive_of_lineage_declared_agent(self):
        meta = _make_meta(parent_agent_id="parent", spawn_reason="subagent")
        meta._live_bindings = []
        result, mock_arch = await self._run_archive(meta, {"agent_id": "agent-uuid"})
        body = _payload(result)
        assert body["success"] is False
        assert body["error_code"] == "AGENT_LOOKS_LIVE"
        assert body["liveness_signals"]  # details are flattened to top level
        mock_arch.assert_not_awaited()  # nothing archived

    @pytest.mark.asyncio
    async def test_refuses_archive_of_agent_with_live_binding(self):
        meta = _make_meta()
        meta._live_bindings = [{"pid": 999}]
        result, mock_arch = await self._run_archive(meta, {"agent_id": "agent-uuid"})
        body = _payload(result)
        assert body["error_code"] == "AGENT_LOOKS_LIVE"
        mock_arch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_overrides_the_guard(self):
        meta = _make_meta(parent_agent_id="parent", spawn_reason="subagent")
        meta._live_bindings = [{"pid": 999}]
        result, mock_arch = await self._run_archive(
            meta, {"agent_id": "agent-uuid", "force": True}
        )
        body = _payload(result)
        assert body["success"] is True
        mock_arch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_force_accepts_string_true(self):
        meta = _make_meta(parent_agent_id="parent", spawn_reason="dispatch")
        meta._live_bindings = []
        result, mock_arch = await self._run_archive(
            meta, {"agent_id": "agent-uuid", "force": "true"}
        )
        assert _payload(result)["success"] is True
        mock_arch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idle_unlineaged_agent_archives_without_force(self):
        meta = _make_meta()  # no lineage, no recent activity
        meta._live_bindings = []
        result, mock_arch = await self._run_archive(meta, {"agent_id": "agent-uuid"})
        assert _payload(result)["success"] is True
        mock_arch.assert_awaited_once()


# ----------------------------------------------------------------------
# archive_old_test_agents preview-first default
# ----------------------------------------------------------------------


class TestArchiveOldTestAgentsPreviewDefault:
    @pytest.mark.asyncio
    async def test_defaults_to_dry_run_when_not_specified(self):
        agent_uuid = "real_001"
        meta = _make_meta(agent_id=agent_uuid, label="real-agent")
        mock_server = MagicMock()
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.load_metadata_async = AsyncMock()
        mock_server.monitors = {}

        with patch("src.mcp_handlers.lifecycle.operations.mcp_server", mock_server), \
             patch("src.agent_lifecycle._agent_age_hours", return_value=1000.0), \
             patch(
                 "src.mcp_handlers.lifecycle.operations._archive_one_agent",
                 new=AsyncMock(return_value=True),
             ) as mock_arch:
            from src.mcp_handlers.lifecycle.operations import handle_archive_old_test_agents
            # include_all but NO dry_run key — must preview, not execute.
            result = await handle_archive_old_test_agents({"include_all": True})

        body = _payload(result)
        assert body["dry_run"] is True
        assert body["total_would_archive"] >= 1  # the stale agent is identified
        mock_arch.assert_not_awaited()  # but nothing is actually archived

    @pytest.mark.asyncio
    async def test_dry_run_false_executes(self):
        agent_uuid = "real_002"
        meta = _make_meta(agent_id=agent_uuid, label="real-agent")
        mock_server = MagicMock()
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.load_metadata_async = AsyncMock()
        mock_server.monitors = {}

        with patch("src.mcp_handlers.lifecycle.operations.mcp_server", mock_server), \
             patch("src.agent_lifecycle._agent_age_hours", return_value=1000.0), \
             patch(
                 "src.mcp_handlers.lifecycle.operations._archive_one_agent",
                 new=AsyncMock(return_value=True),
             ) as mock_arch:
            from src.mcp_handlers.lifecycle.operations import handle_archive_old_test_agents
            result = await handle_archive_old_test_agents(
                {"include_all": True, "dry_run": False}
            )

        body = _payload(result)
        assert body["dry_run"] is False
        mock_arch.assert_awaited()  # execution path taken
