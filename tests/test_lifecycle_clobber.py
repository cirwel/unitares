"""
Regression tests for P011 clobber bug in lifecycle handlers.

Watcher pattern P011: mutations to in-memory AgentMetadata fields
(paused_at, last_response_at, response_completed, recovery_attempt_at,
lifecycle_events) are not persisted by agent_storage.update_agent().
The next load_metadata_async(force=True) wipes them back to defaults.

Observable impact:
- agent_loop_detection reads recovery_attempt_at to grant a 120s grace
  period after self-recovery. If clobbered, no grace → immediate re-pause.
- phases.py + agent_auth.py read paused_at for update routing + auth.
- silence detector reads lifecycle_events for check-in history.

These tests assert the handlers persist runtime state via
agent_storage.persist_runtime_state() so values survive force-reload.
"""

import pytest
from tests.lifecycle_auth import operator_caller, no_operator, bound_caller
import sys
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.helpers import (
    make_agent_meta,
    make_mock_server,
    make_monitor,
    patch_agent_storage,
    patch_lifecycle_server,
)


AGENT_ID = "agent-under-test"


def _server_with_agent(meta):
    server = make_mock_server()
    server.agent_metadata = {AGENT_ID: meta}
    server.monitors = {AGENT_ID: make_monitor()}
    server.get_or_create_monitor = MagicMock(return_value=make_monitor())
    return server


class TestResumeAgentPersistsRuntimeState:

    @pytest.fixture(autouse=True)
    def _as_operator(self):
        """These calls target another agent, so the caller holds the operator token."""
        with operator_caller():
            yield

    @pytest.mark.asyncio
    async def test_resume_persists_paused_at_and_lifecycle_event(self):
        meta = make_agent_meta(status="paused", paused_at="2026-04-16T10:00:00+00:00")
        server = _server_with_agent(meta)

        with patch_agent_storage() as storage, \
             patch_lifecycle_server(server,
                                    require_registered=(AGENT_ID, None),
                                    **{"src.mcp_handlers.lifecycle.operations.resolve_agent_uuid":
                                       MagicMock(return_value=AGENT_ID)}):
            storage.update_agent = AsyncMock(return_value=True)
            storage.persist_runtime_state = AsyncMock(return_value=True)

            from src.mcp_handlers.lifecycle.operations import handle_resume_agent
            await handle_resume_agent({"agent_id": AGENT_ID, "reason": "test"})

            assert storage.persist_runtime_state.await_count == 1, \
                "resume_agent must persist runtime state so paused_at=None and the " \
                "resumed lifecycle event survive the next load_metadata_async(force=True)"
            kwargs = storage.persist_runtime_state.await_args.kwargs
            assert kwargs.get("paused_at") is None
            event = kwargs.get("append_lifecycle_event")
            assert event and event.get("event") == "resumed"


class TestMarkResponseCompletePersistsRuntimeState:
    @pytest.mark.asyncio
    async def test_mark_complete_persists_response_fields(self):
        meta = make_agent_meta(status="active")
        server = _server_with_agent(meta)

        with patch_agent_storage() as storage, \
             patch("src.mcp_handlers.utils.verify_agent_ownership", MagicMock(return_value=True)), \
             patch_lifecycle_server(server,
                                    require_registered=(AGENT_ID, None),
                                    **{"src.mcp_handlers.lifecycle.operations.resolve_agent_uuid":
                                       MagicMock(return_value=AGENT_ID)}):
            storage.update_agent = AsyncMock(return_value=True)
            storage.persist_runtime_state = AsyncMock(return_value=True)

            from src.mcp_handlers.lifecycle.operations import handle_mark_response_complete
            await handle_mark_response_complete({"agent_id": AGENT_ID, "summary": "done"})

            assert storage.persist_runtime_state.await_count >= 1, \
                "mark_response_complete must persist response_completed=True and " \
                "last_response_at so they survive force-reload"
            kwargs = storage.persist_runtime_state.await_args.kwargs
            assert kwargs.get("response_completed") is True
            assert kwargs.get("last_response_at")
            event = kwargs.get("append_lifecycle_event")
            assert event and event.get("event") == "response_completed"


class TestSelfRecoveryReviewPersistsRecoveryAttempt:
    @pytest.mark.asyncio
    async def test_recovery_attempt_at_persisted_before_safety_checks(self):
        meta = make_agent_meta(status="paused", paused_at="2026-04-16T10:00:00+00:00")
        server = _server_with_agent(meta)

        with patch_agent_storage() as storage, \
             patch("src.mcp_handlers.utils.verify_agent_ownership", MagicMock(return_value=True)), \
             patch_lifecycle_server(server,
                                    require_registered=(AGENT_ID, None),
                                    **{"src.mcp_handlers.lifecycle.operations.resolve_agent_uuid":
                                       MagicMock(return_value=AGENT_ID)}):
            storage.update_agent = AsyncMock(return_value=True)
            storage.persist_runtime_state = AsyncMock(return_value=True)

            from src.mcp_handlers.lifecycle.operations import handle_self_recovery_review
            await handle_self_recovery_review({
                "agent_id": AGENT_ID,
                "reflection": "I understand what went wrong and will be more careful.",
            })

            assert storage.persist_runtime_state.await_count >= 1, \
                "self_recovery_review must persist recovery_attempt_at so the loop " \
                "detector's 120s grace window survives any force-reload"
            first_call = storage.persist_runtime_state.await_args_list[0].kwargs
            assert first_call.get("recovery_attempt_at"), \
                "recovery_attempt_at must be persisted BEFORE the safety-check gate"


class TestQuickResumePersistsRecoveryAttempt:
    @pytest.mark.asyncio
    async def test_recovery_attempt_at_persisted_before_safety_checks(self):
        meta = make_agent_meta(status="paused", paused_at="2026-04-16T10:00:00+00:00")
        server = _server_with_agent(meta)

        with patch_agent_storage() as storage, \
             patch("src.mcp_handlers.lifecycle.self_recovery.verify_agent_ownership",
                   MagicMock(return_value=True)), \
             patch_lifecycle_server(server,
                                    require_registered=(AGENT_ID, None),
                                    **{"src.mcp_handlers.lifecycle.self_recovery.resolve_agent_uuid":
                                       MagicMock(return_value=AGENT_ID)}):
            storage.update_agent = AsyncMock(return_value=True)
            storage.persist_runtime_state = AsyncMock(return_value=True)

            from src.mcp_handlers.lifecycle.self_recovery import handle_quick_resume
            await handle_quick_resume({"agent_id": AGENT_ID})

            assert storage.persist_runtime_state.await_count >= 1, \
                "quick_resume must persist recovery_attempt_at before safety checks"
            first_call = storage.persist_runtime_state.await_args_list[0].kwargs
            assert first_call.get("recovery_attempt_at"), \
                "recovery_attempt_at must be persisted BEFORE the safety-check gate"
            assert meta.recovery_attempt_at == first_call["recovery_attempt_at"]


class TestOperatorResumePersistsRuntimeState:

    @pytest.fixture(autouse=True)
    def _as_operator(self):
        """These calls target another agent, so the caller holds the operator token."""
        with operator_caller():
            yield

    @pytest.mark.asyncio
    async def test_operator_resume_persists_paused_at_and_lifecycle_event(self):
        caller_id = "operator-under-test"
        target_meta = make_agent_meta(status="paused", paused_at="2026-04-16T10:00:00+00:00")
        caller_meta = make_agent_meta(label="Operator")
        server = make_mock_server()
        server.agent_metadata = {caller_id: caller_meta, AGENT_ID: target_meta}
        server.monitors = {AGENT_ID: make_monitor()}
        server.get_or_create_monitor = MagicMock(return_value=make_monitor())

        with patch_agent_storage() as storage, \
             patch_lifecycle_server(server,
                                    require_registered=(caller_id, None),
                                    **{"src.mcp_handlers.lifecycle.self_recovery.resolve_agent_uuid":
                                       MagicMock(return_value=caller_id)}):
            storage.update_agent = AsyncMock(return_value=True)
            storage.persist_runtime_state = AsyncMock(return_value=True)

            from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
            await handle_operator_resume_agent({
                "_agent_uuid": caller_id,
                "target_agent_id": AGENT_ID,
                "reason": "test operator recovery",
            })

            storage.update_agent.assert_awaited_once_with(AGENT_ID, status="active")
            storage.persist_runtime_state.assert_awaited_once()
            kwargs = storage.persist_runtime_state.await_args.kwargs
            assert kwargs.get("paused_at") is None
            event = kwargs.get("append_lifecycle_event")
            assert event and event.get("event") == "operator_resumed"


class TestExecuteResolutionPersistsRuntimeState:
    @pytest.mark.asyncio
    async def test_execute_resolution_persists_paused_at_and_lifecycle_event(self):
        from src.dialectic_protocol import Resolution

        meta = make_agent_meta(status="paused", paused_at="2026-04-16T10:00:00+00:00")

        session = MagicMock()
        session.paused_agent_id = AGENT_ID
        session.discovery_id = None
        session.session_id = "sess-1"
        session.dispute_type = None

        resolution = Resolution(
            action="resume",
            conditions=[],
            root_cause="agreed root cause",
            reasoning="merged reasoning",
            signature_a="sig_a",
            signature_b="sig_b",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        mock_server = make_mock_server()
        mock_server.agent_metadata = {AGENT_ID: meta}
        mock_server.load_metadata_async = AsyncMock(return_value=None)

        mock_storage = MagicMock()
        mock_storage.update_agent = AsyncMock(return_value=True)
        mock_storage.persist_runtime_state = AsyncMock(return_value=True)

        with patch("src.mcp_handlers.dialectic.resolution.mcp_server", mock_server), \
             patch("src.agent_storage", mock_storage):
            from src.mcp_handlers.dialectic.resolution import execute_resolution
            await execute_resolution(session, resolution)

        mock_storage.persist_runtime_state.assert_awaited_once()
        kwargs = mock_storage.persist_runtime_state.await_args.kwargs
        assert kwargs.get("paused_at") is None, \
            "execute_resolution must persist paused_at=None so it survives force-reload (P011)"
        event = kwargs.get("append_lifecycle_event")
        assert event and event.get("event") == "resumed"
        assert "agreed root cause" in event.get("reason", "")


class TestPersistRuntimeStateContract:
    """Contract tests for the new agent_storage.persist_runtime_state helper."""

    @pytest.mark.asyncio
    async def test_helper_writes_to_identity_metadata(self):
        from src import agent_storage

        mock_db = MagicMock()
        mock_db.update_identity_metadata = AsyncMock(return_value=True)

        with patch.object(agent_storage, "get_db", return_value=mock_db), \
             patch.object(agent_storage, "_ensure_db_ready", AsyncMock()):
            result = await agent_storage.persist_runtime_state(
                AGENT_ID,
                paused_at=None,
                response_completed=True,
                last_response_at="2026-04-16T10:42:00+00:00",
            )
            assert result is True

        mock_db.update_identity_metadata.assert_awaited_once()
        call_kwargs = mock_db.update_identity_metadata.await_args
        agent_id_arg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs["agent_id"]
        metadata_arg = call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs["metadata"]
        assert agent_id_arg == AGENT_ID
        assert metadata_arg["paused_at"] is None
        assert metadata_arg["response_completed"] is True
        assert metadata_arg["last_response_at"] == "2026-04-16T10:42:00+00:00"

    @pytest.mark.asyncio
    async def test_helper_sentinel_means_no_change(self):
        """Fields not passed must NOT appear in the metadata payload —
        otherwise they'd overwrite existing values with None defaults."""
        from src import agent_storage

        mock_db = MagicMock()
        mock_db.update_identity_metadata = AsyncMock(return_value=True)

        with patch.object(agent_storage, "get_db", return_value=mock_db), \
             patch.object(agent_storage, "_ensure_db_ready", AsyncMock()):
            await agent_storage.persist_runtime_state(
                AGENT_ID,
                response_completed=True,  # only this one
            )

        metadata_arg = mock_db.update_identity_metadata.await_args.args[1] \
            if len(mock_db.update_identity_metadata.await_args.args) > 1 \
            else mock_db.update_identity_metadata.await_args.kwargs["metadata"]
        assert "response_completed" in metadata_arg
        assert "paused_at" not in metadata_arg
        assert "last_response_at" not in metadata_arg
        assert "recovery_attempt_at" not in metadata_arg


class TestLoaderHydratesRuntimeFields:
    @pytest.mark.asyncio
    async def test_loader_reads_runtime_fields_from_identity_metadata(self):
        """Loader must hydrate paused_at / last_response_at / response_completed /
        recovery_attempt_at from agent.metadata so persisted values survive reload."""
        from src import agent_metadata_persistence as persistence

        agent = MagicMock()
        agent.agent_id = AGENT_ID
        agent.status = "paused"
        agent.created_at = None
        agent.last_activity_at = None
        agent.updated_at = None
        agent.tags = []
        agent.notes = ""
        agent.purpose = None
        agent.parent_agent_id = None
        agent.spawn_reason = None
        agent.health_status = "unknown"
        agent.metadata = {
            "paused_at": "2026-04-16T10:00:00+00:00",
            "last_response_at": "2026-04-16T10:30:00+00:00",
            "response_completed": True,
            "recovery_attempt_at": "2026-04-16T10:35:00+00:00",
        }

        from src import agent_storage as storage_mod
        with patch.object(storage_mod, "list_agents", AsyncMock(return_value=[agent])), \
             patch("src.cache.get_metadata_cache", side_effect=Exception("no cache"), create=True):
            result = await persistence._load_metadata_from_postgres_async()

        meta = result[AGENT_ID]
        assert meta.paused_at == "2026-04-16T10:00:00+00:00"
        assert meta.last_response_at == "2026-04-16T10:30:00+00:00"
        assert meta.response_completed is True
        assert meta.recovery_attempt_at == "2026-04-16T10:35:00+00:00"


class TestCircuitBreakerPersistsRuntimeState:
    """The pause actuator publishes status, token and event atomically."""

    @pytest.mark.asyncio
    async def test_pause_decision_persists_generation_and_lifecycle_event(self):
        from src import agent_loop_detection as ald
        from src import agent_storage

        meta = make_agent_meta(status="active")
        meta.recent_update_timestamps = []
        meta.recent_decisions = []
        meta.loop_cooldown_until = None
        meta.add_recent_update = MagicMock()
        meta.add_lifecycle_event = MagicMock()

        monitor = make_monitor()
        monitor.process_update = MagicMock(return_value={
            "decision": {
                "action": "pause",
                "reason": "UNITARES high-risk verdict (risk_score=0.65)",
            },
            "metrics": {"coherence": 0.45, "risk_score": 0.65},
        })

        mock_db = MagicMock()
        mock_db.increment_update_count = AsyncMock(return_value=42)

        with patch.object(ald, "agent_metadata", {AGENT_ID: meta}), \
             patch.object(ald, "monitors", {AGENT_ID: monitor}), \
             patch.object(ald, "verify_agent_ownership", return_value=(True, None)), \
             patch.object(ald, "detect_loop_pattern", return_value=(False, "")), \
             patch("src.agent_lifecycle.get_or_create_monitor", return_value=monitor), \
             patch.object(agent_storage, "get_db", return_value=mock_db), \
             patch.object(agent_storage, "pause_agent_with_token",
                          AsyncMock(return_value={
                              "applied": True,
                              "pause_token": "durable",
                          })) as pause_mock, \
             patch.object(ald, "save_monitor_state_async", AsyncMock()):
            # Disable auto-recovery to avoid touching unrelated code paths
            with patch.dict("os.environ", {"UNITARES_AUTO_DIALECTIC_RECOVERY": "0"}):
                result = await ald.process_update_authenticated_async(
                    AGENT_ID, "test-key", {"task_type": "mixed"}
                )

        pause_mock.assert_awaited_once()
        kwargs = pause_mock.await_args.kwargs
        assert kwargs.get("pause_token")
        event = kwargs.get("lifecycle_event")
        assert event and event.get("event") == "paused"
        assert "high-risk" in (event.get("reason") or ""), \
            "lifecycle event reason must carry the decision reason"
        assert event.get("actuation_id"), \
            "persisted pause event must carry the runtime actuation id"
        assert result["enforcement"]["actuation_id"] == event["actuation_id"]
        assert result["enforcement"]["applied_at"] == kwargs["pause_token"]
        assert result["enforcement"]["durable"] is True
        meta.add_lifecycle_event.assert_called_once_with(
            "paused",
            "UNITARES high-risk verdict (risk_score=0.65)",
            actuation_id=event["actuation_id"],
        )

    @pytest.mark.asyncio
    async def test_pause_storage_failure_stays_local_and_skips_auto_recovery(self):
        from src import agent_loop_detection as ald
        from src import agent_storage

        meta = make_agent_meta(status="active")
        meta.recent_update_timestamps = []
        meta.recent_decisions = []
        meta.loop_cooldown_until = None
        meta.add_recent_update = MagicMock()
        meta.add_lifecycle_event = MagicMock()
        monitor = make_monitor()
        monitor.process_update = MagicMock(return_value={
            "decision": {"action": "pause", "reason": "high risk"},
            "metrics": {"coherence": 0.45, "risk_score": 0.65},
        })
        mock_db = MagicMock()
        mock_db.increment_update_count = AsyncMock(return_value=42)
        auto_recovery = AsyncMock()

        with patch.object(ald, "agent_metadata", {AGENT_ID: meta}), \
             patch.object(ald, "monitors", {AGENT_ID: monitor}), \
             patch.object(ald, "verify_agent_ownership", return_value=(True, None)), \
             patch.object(ald, "detect_loop_pattern", return_value=(False, "")), \
             patch("src.agent_lifecycle.get_or_create_monitor", return_value=monitor), \
             patch.object(agent_storage, "get_db", return_value=mock_db), \
             patch.object(
                 agent_storage,
                 "pause_agent_with_token",
                 AsyncMock(side_effect=RuntimeError("database unavailable")),
             ), \
             patch.object(ald, "_auto_initiate_dialectic_recovery", auto_recovery), \
             patch.object(ald, "save_monitor_state_async", AsyncMock()):
            with patch.dict("os.environ", {"UNITARES_AUTO_DIALECTIC_RECOVERY": "1"}):
                result = await ald.process_update_authenticated_async(
                    AGENT_ID, "test-key", {"task_type": "mixed"}
                )

        assert meta.status == "paused"
        assert meta.paused_at
        assert result["enforcement"]["durable"] is False
        assert result["enforcement"]["effect"] == "in_memory_pause_only"
        assert result["enforcement"]["persistence_error"] == "RuntimeError"
        assert result["auto_recovery_initiated"] is False
        auto_recovery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_durable_pause_generation_is_reused(self):
        from src import agent_loop_detection as ald
        from src import agent_storage

        meta = make_agent_meta(status="active")
        meta.recent_update_timestamps = []
        meta.recent_decisions = []
        meta.loop_cooldown_until = None
        meta.add_recent_update = MagicMock()
        meta.add_lifecycle_event = MagicMock()
        monitor = make_monitor()
        monitor.process_update = MagicMock(return_value={
            "decision": {"action": "pause", "reason": "high risk"},
            "metrics": {"coherence": 0.45, "risk_score": 0.65},
        })
        mock_db = MagicMock()
        mock_db.increment_update_count = AsyncMock(return_value=42)

        with patch.object(ald, "agent_metadata", {AGENT_ID: meta}), \
             patch.object(ald, "monitors", {AGENT_ID: monitor}), \
             patch.object(ald, "verify_agent_ownership", return_value=(True, None)), \
             patch.object(ald, "detect_loop_pattern", return_value=(False, "")), \
             patch("src.agent_lifecycle.get_or_create_monitor", return_value=monitor), \
             patch.object(agent_storage, "get_db", return_value=mock_db), \
             patch.object(
                 agent_storage,
                 "pause_agent_with_token",
                 AsyncMock(return_value={
                     "applied": False,
                     "reason": "already_paused_different_generation",
                     "current_pause_token": "pause-existing",
                 }),
             ), \
             patch.object(ald, "save_monitor_state_async", AsyncMock()):
            with patch.dict("os.environ", {"UNITARES_AUTO_DIALECTIC_RECOVERY": "0"}):
                result = await ald.process_update_authenticated_async(
                    AGENT_ID, "test-key", {"task_type": "mixed"}
                )

        assert meta.status == "paused"
        assert meta.paused_at == "pause-existing"
        assert result["enforcement"]["durable"] is True
        assert result["enforcement"]["reused_pause_generation"] is True
        assert (
            result["enforcement"]["effect"]
            == "durable_pause_generation_already_present"
        )
        meta.add_lifecycle_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_safety_net_resume_persists_paused_at_clear_and_event(self):
        from src import agent_loop_detection as ald
        from src import agent_storage

        meta = make_agent_meta(status="paused", paused_at="2026-04-16T22:16:36+00:00")
        pause_token = meta.paused_at
        meta.loop_cooldown_until = None
        meta.loop_detected_at = None
        meta.recent_update_timestamps = []
        meta.recent_decisions = []
        meta.add_lifecycle_event = MagicMock()

        monitor = make_monitor(coherence=0.55, mean_risk=0.40)

        with patch.object(ald, "agent_metadata", {AGENT_ID: meta}), \
             patch.object(ald, "monitors", {AGENT_ID: monitor}), \
             patch.object(
                 agent_storage,
                 "resume_agent_if_pause_token",
                 AsyncMock(return_value={
                     "applied": True,
                     "pause_token": pause_token,
                     "dialectic_conditions": [],
                 }),
             ) as resume_mock, \
             patch.object(
                 agent_storage,
                 "persist_runtime_state",
                 AsyncMock(),
             ) as persist_mock:
            await ald._safety_net_resume(AGENT_ID, reason="dialectic-failed")

        assert meta.status == "active"
        resume_mock.assert_awaited_once()
        kwargs = resume_mock.await_args.kwargs
        assert kwargs["expected_pause_token"] == pause_token
        assert kwargs["dialectic_condition_additions"] == []
        assert kwargs["lifecycle_event"]["event"] == "safety_net_resumed"
        persist_mock.assert_not_awaited()
        meta.add_lifecycle_event.assert_called_once_with(
            "safety_net_resumed",
            kwargs["lifecycle_event"]["reason"],
        )

    @pytest.mark.asyncio
    async def test_safety_net_resume_refusal_leaves_pause_untouched(self):
        from src import agent_loop_detection as ald
        from src import agent_storage

        pause_token = "2026-04-16T22:16:36+00:00"
        meta = make_agent_meta(status="paused", paused_at=pause_token)
        meta.add_lifecycle_event = MagicMock()
        monitor = make_monitor(coherence=0.55, mean_risk=0.40)

        with patch.object(ald, "agent_metadata", {AGENT_ID: meta}), \
             patch.object(ald, "monitors", {AGENT_ID: monitor}), \
             patch.object(
                 agent_storage,
                 "resume_agent_if_pause_token",
                 AsyncMock(return_value={
                     "applied": False,
                     "reason": "pause_generation_superseded",
                     "current_pause_token": "newer-pause",
                 }),
             ) as resume_mock:
            await ald._safety_net_resume(AGENT_ID, reason="dialectic-failed")

        resume_mock.assert_awaited_once()
        assert meta.status == "paused"
        assert meta.paused_at == pause_token
        meta.add_lifecycle_event.assert_not_called()
