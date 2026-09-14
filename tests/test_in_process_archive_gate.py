"""Tests for the in-process archive gate in process_update_authenticated_async.

An in-process caller bypasses handle_process_agent_update and calls
process_update_authenticated_async directly; the first was Steward, from the
since-retired unitares-pi-plugin. Before this gate, an archived Steward identity
would still accept updates — silent resurrection via
a different code path than the one sticky-archive guards on the MCP tool path.

Regression scope: ensure archived agents are refused at the
process_update_authenticated_async entrypoint regardless of caller.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_metadata(status: str = "active"):
    return SimpleNamespace(
        status=status,
        api_key="test-key",
        recent_update_timestamps=[],
        recent_decisions=[],
        loop_cooldown_until=None,
        recovery_attempt_at=None,
        created_at=datetime.now().isoformat(),
        tags=[],
        paused_at=None,
        loop_detected_at=None,
        loop_incidents=[],
        total_updates=10,
        last_update=datetime.now().isoformat(),
        archived_at=None,
    )


class TestInProcessArchiveGate:
    """process_update_authenticated_async must refuse archived agents.

    The MCP-tool path has its own gate earlier (phases.py sticky-archive).
    This covers the in-process path, which bypasses that gate.
    """

    @pytest.mark.asyncio
    async def test_archived_agent_raises_value_error(self):
        agent_id = "test-uuid-archived-in-process"
        meta = _make_metadata(status="archived")

        with patch(
            "src.agent_loop_detection.agent_metadata",
            {agent_id: meta},
        ), patch(
            "src.agent_loop_detection.verify_agent_ownership",
            return_value=(True, ""),
        ):
            from src.agent_loop_detection import process_update_authenticated_async
            with pytest.raises(ValueError, match="archived"):
                await process_update_authenticated_async(
                    agent_id=agent_id,
                    api_key="test-key",
                    agent_state={},
                )

    @pytest.mark.asyncio
    async def test_active_agent_not_blocked_by_archive_gate(self):
        """Sanity: active agents pass the gate (loop-detection path still runs)."""
        agent_id = "test-uuid-active-in-process"
        meta = _make_metadata(status="active")

        # Patch detect_loop_pattern to return no-loop so we exit cleanly right
        # after the archive check. The function then hits get_or_create_monitor
        # etc. which we don't want to wire up — raise a sentinel instead.
        def _sentinel_loop(agent_id):
            raise AssertionError("reached-loop-detection-phase")

        with patch(
            "src.agent_loop_detection.agent_metadata",
            {agent_id: meta},
        ), patch(
            "src.agent_loop_detection.verify_agent_ownership",
            return_value=(True, ""),
        ), patch(
            "src.agent_loop_detection.detect_loop_pattern",
            side_effect=_sentinel_loop,
        ):
            from src.agent_loop_detection import process_update_authenticated_async
            # Expect our sentinel — meaning we passed the archive gate.
            with pytest.raises(AssertionError, match="reached-loop-detection-phase"):
                await process_update_authenticated_async(
                    agent_id=agent_id,
                    api_key="test-key",
                    agent_state={},
                )

    @pytest.mark.asyncio
    async def test_unknown_agent_not_blocked_by_archive_gate(self):
        """Sanity: agents missing from the in-memory dict aren't treated as archived."""
        agent_id = "test-uuid-not-in-dict"

        def _sentinel_loop(agent_id):
            raise AssertionError("reached-loop-detection-phase")

        with patch(
            "src.agent_loop_detection.agent_metadata",
            {},  # agent not present
        ), patch(
            "src.agent_loop_detection.verify_agent_ownership",
            return_value=(True, ""),
        ), patch(
            "src.agent_loop_detection.detect_loop_pattern",
            side_effect=_sentinel_loop,
        ):
            from src.agent_loop_detection import process_update_authenticated_async
            with pytest.raises(AssertionError, match="reached-loop-detection-phase"):
                await process_update_authenticated_async(
                    agent_id=agent_id,
                    api_key="test-key",
                    agent_state={},
                )
