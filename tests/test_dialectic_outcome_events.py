"""
Closes the dialectic→outcome_events tracking gap surfaced by the
2026-05-06 council review: 0 outcome_events referenced any dialectic
session_id across 47 historical sessions, leaving 98.5% agrees=True
resolutions with no downstream coupling. execute_resolution now emits
a neutral `dialectic_resolved` outcome_event so back-tests can correlate
resumption with subsequent agent state.
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.helpers import make_agent_meta, make_mock_server


AGENT_ID = "paused-agent-under-test"


def _resolution(conditions=None, root_cause="agreed root cause"):
    from src.dialectic_protocol import Resolution
    return Resolution(
        action="resume",
        conditions=conditions or [],
        root_cause=root_cause,
        reasoning="merged reasoning",
        signature_a="sig_a",
        signature_b="sig_b",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _session(session_type="recovery", discovery_id=None, synthesis_round=1):
    session = MagicMock()
    session.paused_agent_id = AGENT_ID
    session.discovery_id = discovery_id
    session.session_id = "dialectic-sess-abc123"
    session.dispute_type = None
    session.session_type = session_type
    session.synthesis_round = synthesis_round
    return session


def _patch_targets(emit_mock):
    meta = make_agent_meta(status="paused", paused_at="2026-04-16T10:00:00+00:00")
    server = make_mock_server()
    server.agent_metadata = {AGENT_ID: meta}
    server.load_metadata_async = AsyncMock(return_value=None)
    storage = MagicMock()
    storage.update_agent = AsyncMock(return_value=True)
    storage.persist_runtime_state = AsyncMock(return_value=True)
    return server, storage, patch.multiple(
        "src.mcp_handlers.dialectic.resolution",
        mcp_server=server,
        _record_outcome_event_inline=emit_mock,
    ), patch("src.agent_storage", storage)


class TestExecuteResolutionEmitsOutcomeEvent:
    @pytest.mark.asyncio
    async def test_emits_dialectic_resolved_with_session_id_in_detail(self):
        emit = AsyncMock(return_value={"outcome_id": "evt-1"})
        session = _session(synthesis_round=2)
        resolution = _resolution(conditions=["monitor for 24h", "reduce complexity to 0.3"])

        _, _, p_module, p_storage = _patch_targets(emit)
        with p_module, p_storage:
            from src.mcp_handlers.dialectic.resolution import execute_resolution
            result = await execute_resolution(session, resolution)

        assert result["success"] is True
        emit.assert_awaited_once()
        args = emit.await_args.args[0]
        assert args["outcome_type"] == "dialectic_resolved"
        assert args["agent_id"] == AGENT_ID
        assert args["is_bad"] is False
        assert args["outcome_score"] == 1.0
        assert args["decision_action"] == "proceed"
        # Phase 1 (migration 038): dialectic resolution is computed
        # server-side from session protocol state, not the agent's claim.
        # Architect council 2026-05-19 flagged it as peer-mediated, which
        # the v1 enum has no value for; server_observation is the honest
        # placement pending v2 taxonomy redesign.
        assert args["verification_source"] == "server_observation"
        detail = args["detail"]
        assert detail["dialectic_session_id"] == "dialectic-sess-abc123"
        assert detail["session_type"] == "recovery"
        assert detail["root_cause"] == "agreed root cause"
        assert detail["conditions"] == ["monitor for 24h", "reduce complexity to 0.3"]
        assert detail["synthesis_round"] == 2
        assert detail["status_changed"] is True
        assert "resolution_hash" in detail

    @pytest.mark.asyncio
    async def test_emit_failure_does_not_break_resolution(self):
        emit = AsyncMock(side_effect=RuntimeError("simulated db outage"))
        session = _session()
        resolution = _resolution()

        _, storage, p_module, p_storage = _patch_targets(emit)
        with p_module, p_storage:
            from src.mcp_handlers.dialectic.resolution import execute_resolution
            result = await execute_resolution(session, resolution)

        assert result["success"] is True
        assert result["new_status"] == "active"
        emit.assert_awaited_once()
        storage.persist_runtime_state.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_conditions_applied_count_excludes_failed(self):
        emit = AsyncMock(return_value={"outcome_id": "evt-2"})
        session = _session()
        resolution = _resolution(conditions=["c1", "c2", "c3"])

        _, _, p_module, p_storage = _patch_targets(emit)
        from src.mcp_handlers.dialectic import resolution as resolution_mod

        async def fake_apply(parsed, agent_id, server):
            cond = parsed.get("raw") if isinstance(parsed, dict) else None
            return {"condition": cond, "status": "failed" if cond == "c2" else "applied"}

        with p_module, p_storage, \
             patch.object(resolution_mod, "parse_condition", side_effect=lambda c: {"raw": c}), \
             patch.object(resolution_mod, "apply_condition", side_effect=fake_apply):
            await resolution_mod.execute_resolution(session, resolution)

        detail = emit.await_args.args[0]["detail"]
        assert detail["conditions_total"] == 3
        assert detail["conditions_applied"] == 2  # c2 failed
