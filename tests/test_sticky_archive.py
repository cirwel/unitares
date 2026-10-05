"""Tests for sticky-archive guard: manual-intent + cooldown window.

Regression coverage for the 2026-04-18 incident where acd8a774 was
manually archived at 01:29:33 and resurrected 10 seconds later via
process_agent_update, then circuit-broke ~45 min later.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.lifecycle_auth import operator_caller

from tests.helpers import parse_result
from tests.test_core_update import (
    _make_metadata,
    _make_mock_mcp_server,
    _make_monitor,
)


# ----------------------------------------------------------------------
# Local helpers — duplicated from test_core_update.TestProcessAgentUpdate
# to avoid pytest re-running the parent class's tests via inheritance.
# ----------------------------------------------------------------------


def _common_patches(mock_server, agent_uuid="test-uuid-1234",
                    context_agent_id=None, context_session_key="session-1"):
    ctx_agent = context_agent_id or agent_uuid
    mock_storage = MagicMock(
        update_agent=AsyncMock(),
        get_agent=AsyncMock(return_value=None),
        get_or_create_agent=AsyncMock(
            return_value=(MagicMock(api_key="test-key"), True)
        ),
        record_agent_state=AsyncMock(),
        create_agent=AsyncMock(),
    )
    return {
        "mcp_server": mock_server,
        "ctx_agent_id": ctx_agent,
        "ctx_session_key": context_session_key,
        "storage": mock_storage,
    }


def _apply_patches(patches_dict):
    stack = ExitStack()
    stack.enter_context(patch("src.mcp_handlers.core.mcp_server", patches_dict["mcp_server"]))
    stack.enter_context(patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value=patches_dict["ctx_agent_id"],
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.context.get_context_session_key",
        return_value=patches_dict["ctx_session_key"],
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.identity.handlers.ensure_agent_persisted",
        new_callable=AsyncMock, return_value=False,
    ))
    stack.enter_context(patch(
        "src.mcp_handlers.updates.phases.agent_storage",
        patches_dict["storage"],
    ))
    return stack


@pytest.fixture
def mock_server():
    return _make_mock_mcp_server()


@pytest.fixture
def mock_monitor():
    return _make_monitor()


# ----------------------------------------------------------------------
# Archived agents are always refused. Stage 3 (PR after #39) removed the
# auto-resume branch that previously rescued sweep-archived residents —
# that rescue is no longer needed because residents self-tag 'persistent'.
# Manual archives were already refused via the sticky marker. This class
# now verifies the uniform refusal regardless of how the archive happened.
# ----------------------------------------------------------------------


class TestArchivedRefusal:
    """Any archived agent hitting process_agent_update gets an error response."""

    @pytest.mark.asyncio
    async def test_recent_archive_is_refused(
        self, mock_server, mock_monitor
    ):
        """Archive seconds ago (the 2026-04-18 incident shape) is refused."""
        agent_uuid = "test-uuid-recent-archive"
        recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        meta = _make_metadata(status="archived", total_updates=5)
        meta.archived_at = recent
        meta.notes = ""
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = _common_patches(mock_server, agent_uuid=agent_uuid)
        with _apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "Immediate resurrection attempt.",
                "response_mode": "full",
            })
            data = parse_result(result)

        assert data["success"] is False
        assert "archived" in data["error"].lower()
        assert data["context"]["status"] == "archived"

    @pytest.mark.asyncio
    async def test_old_archive_is_still_refused(
        self, mock_server, mock_monitor
    ):
        """Archive days ago with many updates is ALSO refused.

        Pre-Stage 3 this path auto-resumed to rescue sweep-archived
        residents. Now residents carry the 'persistent' tag (PR #39) so
        sweep can't archive them, and auto-resume for other archived
        agents would mask real bugs. Recovery requires explicit
        self_recovery() or onboard(force_new=true).
        """
        agent_uuid = "test-uuid-old-archive"
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        meta = _make_metadata(status="archived", total_updates=50)
        meta.archived_at = old
        meta.notes = ""
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = _common_patches(mock_server, agent_uuid=agent_uuid)
        with _apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "Old archive — no auto-resume anymore.",
                "response_mode": "full",
            })
            data = parse_result(result)

        assert data["success"] is False
        assert "archived" in data["error"].lower()
        assert "self_recovery" in data["recovery"]["action"]


# ----------------------------------------------------------------------
# Task 5: handle_archive_agent stamps notes so existing gate trips.
# ----------------------------------------------------------------------


class TestManualArchiveMarker:
    """handle_archive_agent must stamp meta.notes with 'user requested' marker."""

    @pytest.fixture(autouse=True)
    def _as_operator(self):
        """These calls target another agent, so the caller holds the operator token."""
        with operator_caller():
            yield


    @pytest.mark.asyncio
    async def test_manual_archive_stamps_user_requested_marker(self):
        """After handle_archive_agent, meta.notes must contain 'user requested'."""
        from types import SimpleNamespace

        agent_uuid = "cccccccc-0000-4000-8000-000000000169"
        meta = SimpleNamespace(
            agent_id=agent_uuid,
            status="active",
            archived_at=None,
            notes="",
            total_updates=5,
        )
        meta.add_lifecycle_event = MagicMock()

        mock_server = MagicMock()
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.load_metadata_async = AsyncMock()
        mock_server.monitors = {}

        mock_storage = MagicMock(update_agent=AsyncMock(return_value=True))

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
             ), \
             patch(
                 "src.mcp_handlers.lifecycle.mutation._invalidate_agent_cache",
                 new=AsyncMock(),
             ):
            from src.mcp_handlers.lifecycle.mutation import handle_archive_agent
            await handle_archive_agent({
                "agent_id": agent_uuid,
                "reason": "Manual archive",
            })

        assert "user requested" in meta.notes.lower(), (
            f"handle_archive_agent must stamp meta.notes with 'user requested' "
            f"marker so phases.py:337 gate catches it. Got notes={meta.notes!r}"
        )

        # P011 guard: the marker must be persisted via update_agent, not just
        # mutated in-memory. Otherwise it's clobbered on next metadata reload.
        mock_storage.update_agent.assert_awaited_once()
        call_kwargs = mock_storage.update_agent.await_args.kwargs
        assert "user requested" in (call_kwargs.get("notes") or "").lower(), (
            f"update_agent must be called with notes containing the marker. "
            f"Got kwargs={call_kwargs}"
        )

    @pytest.mark.asyncio
    async def test_meta_notes_not_mirrored_when_persist_fails(self):
        """If update_agent raises, meta.notes must NOT be mirrored.

        Otherwise in-memory diverges from DB: the next load_metadata_async
        reload clobbers the mirror back to the persisted (empty) value,
        and the sticky marker vanishes silently (P011, fingerprint ad43c067).
        """
        from types import SimpleNamespace

        agent_uuid = "cccccccc-0000-4000-8000-000000000234"
        meta = SimpleNamespace(
            agent_id=agent_uuid,
            status="active",
            archived_at=None,
            notes="",
            total_updates=5,
        )
        meta.add_lifecycle_event = MagicMock()

        mock_server = MagicMock()
        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.load_metadata_async = AsyncMock()
        mock_server.monitors = {}

        mock_storage = MagicMock(
            update_agent=AsyncMock(side_effect=RuntimeError("db down"))
        )

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
             ), \
             patch(
                 "src.mcp_handlers.lifecycle.mutation._invalidate_agent_cache",
                 new=AsyncMock(),
             ):
            from src.mcp_handlers.lifecycle.mutation import handle_archive_agent
            await handle_archive_agent({
                "agent_id": agent_uuid,
                "reason": "Manual archive",
            })

        assert meta.notes == "", (
            f"Persistence failed, so meta.notes must stay in sync with DB (empty). "
            f"Got: {meta.notes!r}"
        )
        mock_storage.update_agent.assert_awaited_once()


# ----------------------------------------------------------------------
# Task 7: Full incident replay — archive then 10s-later update is refused.
# ----------------------------------------------------------------------


class TestIncidentReplay:
    """Replay the 2026-04-18 acd8a774 incident timeline."""

    @pytest.mark.asyncio
    async def test_archive_then_immediate_update_is_refused(
        self, mock_server, mock_monitor
    ):
        """Manual archive → 10s later process_agent_update → MUST NOT auto-resume.

        Exact timeline from the incident:
        - T+0: manual archive (stamps 'user requested' marker + sets archived_at)
        - T+10s: process_agent_update arrives with same agent_uuid
        - Expected: error response (blocked by manual marker AND cooldown)
        """
        agent_uuid = "test-uuid-incident-acd8a774"

        # Simulate post-archive state as handle_archive_agent would leave it.
        archived_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        meta = _make_metadata(status="archived", total_updates=12)
        meta.archived_at = archived_at
        meta.notes = "user requested archive: Manual archive"

        mock_server.agent_metadata = {agent_uuid: meta}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        mock_server.monitors = {agent_uuid: mock_monitor}

        p = _common_patches(mock_server, agent_uuid=agent_uuid)
        with _apply_patches(p):
            from src.mcp_handlers.core import handle_process_agent_update
            result = await handle_process_agent_update({
                "response_text": "Incident: stale client resuming after archive.",
                "response_mode": "full",
            })
            data = parse_result(result)

        assert data["success"] is False
        error_text = data["error"].lower()
        # Stage 3 collapsed all archive-refusal reasons into a single message.
        # The detail (marker, cooldown, etc.) isn't part of the error text
        # anymore — the refusal itself is what matters.
        assert "archived" in error_text and "cannot" in error_text
        assert data["context"]["status"] == "archived"
