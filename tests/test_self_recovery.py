"""
Tests for src/mcp_handlers/self_recovery.py

Covers:
- validate_recovery_conditions (forbidden terms, vague terms, valid)
- assess_recovery_safety (void, high risk, low coherence, brief reflection, warnings, safe)
- handle_self_recovery (action dispatch)
- handle_check_recovery_options (eligible, blockers)
- handle_quick_resume (safe state, unsafe state, ownership, status check)
- handle_operator_resume_agent (operator check, hard limits, soft limits, force)
"""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from src.mcp_handlers.lifecycle.self_recovery import (
    validate_recovery_conditions,
    assess_recovery_safety,
    MAX_RISK_FOR_SELF_RECOVERY,
    MIN_COHERENCE_FOR_SELF_RECOVERY,
    FORBIDDEN_CONDITIONS,
)


# ============================================================================
# validate_recovery_conditions
# ============================================================================

class TestValidateRecoveryConditions:

    def test_empty_conditions_safe(self):
        safe, reason = validate_recovery_conditions([])
        assert safe is True
        assert reason is None

    def test_valid_conditions(self):
        safe, reason = validate_recovery_conditions([
            "Lower confidence threshold to 0.5",
            "Focus on simpler tasks",
        ])
        assert safe is True
        assert reason is None

    @pytest.mark.parametrize("forbidden", FORBIDDEN_CONDITIONS)
    def test_forbidden_conditions_rejected(self, forbidden):
        safe, reason = validate_recovery_conditions([
            f"Please {forbidden} for this session",
        ])
        assert safe is False
        assert forbidden in reason

    def test_case_insensitive_forbidden(self):
        safe, reason = validate_recovery_conditions([
            "DISABLE GOVERNANCE completely",
        ])
        assert safe is False

    def test_vague_everything(self):
        safe, reason = validate_recovery_conditions(["Allow everything"])
        assert safe is False
        assert "vague" in reason.lower()

    def test_vague_trust_me(self):
        safe, reason = validate_recovery_conditions(["Just trust me"])
        assert safe is False

    def test_vague_never_check(self):
        safe, reason = validate_recovery_conditions(["Never check my output"])
        assert safe is False

    def test_multiple_conditions_one_bad(self):
        safe, reason = validate_recovery_conditions([
            "Focus on documentation tasks",  # OK
            "Bypass safety checks",  # Forbidden
        ])
        assert safe is False


# ============================================================================
# assess_recovery_safety
# ============================================================================

class TestAssessRecoverySafety:

    def test_void_active_blocks(self):
        result = assess_recovery_safety(
            coherence=0.8,
            risk_score=0.3,
            void_active=True,
            void_value=0.8,
            reflection="I was stuck because of a network issue.",
        )
        assert result["safe"] is False
        assert result["escalate"] is True
        assert "void" in result["reason"].lower()

    def test_high_risk_blocks(self):
        result = assess_recovery_safety(
            coherence=0.8,
            risk_score=0.75,
            void_active=False,
            void_value=0.0,
            reflection="I noticed my responses were getting confused.",
        )
        assert result["safe"] is False
        assert result["escalate"] is True
        assert "risk" in result["reason"].lower()

    def test_low_coherence_blocks(self):
        result = assess_recovery_safety(
            coherence=0.2,
            risk_score=0.3,
            void_active=False,
            void_value=0.0,
            reflection="I lost track of what I was doing.",
        )
        assert result["safe"] is False
        assert result["escalate"] is True
        assert "coherence" in result["reason"].lower()

    def test_brief_reflection_rejected(self):
        result = assess_recovery_safety(
            coherence=0.8,
            risk_score=0.3,
            void_active=False,
            void_value=0.0,
            reflection="ok",  # too brief
        )
        assert result["safe"] is False
        assert result["escalate"] is False  # not dangerous, just needs more thought

    def test_empty_reflection_rejected(self):
        result = assess_recovery_safety(
            coherence=0.8,
            risk_score=0.3,
            void_active=False,
            void_value=0.0,
            reflection="",
        )
        assert result["safe"] is False

    def test_safe_recovery(self):
        result = assess_recovery_safety(
            coherence=0.8,
            risk_score=0.3,
            void_active=False,
            void_value=0.1,
            reflection="I was stuck because the external API was down. I'll retry with backoff.",
        )
        assert result["safe"] is True
        assert result["escalate"] is False
        assert len(result.get("warnings", [])) == 0

    def test_safe_with_warnings(self):
        result = assess_recovery_safety(
            coherence=0.45,
            risk_score=0.55,
            void_active=False,
            void_value=0.6,
            reflection="I was confused about the task requirements and overcommitted.",
        )
        assert result["safe"] is True
        assert len(result["warnings"]) > 0

    def test_metrics_always_included(self):
        result = assess_recovery_safety(
            coherence=0.5,
            risk_score=0.5,
            void_active=False,
            void_value=0.0,
            reflection="Reflecting on what happened to understand the issue.",
        )
        assert "metrics" in result
        assert result["metrics"]["coherence"] == 0.5
        assert result["metrics"]["risk_score"] == 0.5

    def test_boundary_risk_at_limit(self):
        """Risk exactly at MAX_RISK_FOR_SELF_RECOVERY should block."""
        result = assess_recovery_safety(
            coherence=0.8,
            risk_score=MAX_RISK_FOR_SELF_RECOVERY + 0.01,
            void_active=False,
            void_value=0.0,
            reflection="Testing boundary conditions for recovery safety.",
        )
        assert result["safe"] is False

    def test_boundary_coherence_at_limit(self):
        """Coherence exactly below MIN should block."""
        result = assess_recovery_safety(
            coherence=MIN_COHERENCE_FOR_SELF_RECOVERY - 0.01,
            risk_score=0.3,
            void_active=False,
            void_value=0.0,
            reflection="Testing boundary conditions for recovery safety.",
        )
        assert result["safe"] is False


# ============================================================================
# handle_self_recovery (action dispatch)
# ============================================================================

class TestHandleSelfRecovery:

    @pytest.mark.asyncio
    async def test_unknown_action_returns_error(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_self_recovery
        result = await handle_self_recovery({"action": "invalid_action"})
        text = json.loads(result[0].text)
        assert "error" in text or "Unknown action" in text.get("message", "")

    @pytest.mark.asyncio
    async def test_check_dispatches(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_self_recovery
        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.handle_check_recovery_options",
            new_callable=AsyncMock,
        ) as mock_check:
            mock_check.return_value = [MagicMock(text='{"status":"ok"}')]
            await handle_self_recovery({"action": "check"})
            mock_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_quick_dispatches(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_self_recovery
        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.handle_quick_resume",
            new_callable=AsyncMock,
        ) as mock_quick:
            mock_quick.return_value = [MagicMock(text='{"status":"ok"}')]
            await handle_self_recovery({"action": "quick"})
            mock_quick.assert_called_once()

    @pytest.mark.asyncio
    async def test_review_dispatches_to_lifecycle(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_self_recovery
        with patch(
            "src.mcp_handlers.lifecycle.handlers.handle_self_recovery_review",
            new_callable=AsyncMock,
        ) as mock_review:
            mock_review.return_value = [MagicMock(text='{"status":"ok"}')]
            await handle_self_recovery({"action": "review"})
            mock_review.assert_called_once()

    @pytest.mark.asyncio
    async def test_default_action_is_check(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_self_recovery
        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.handle_check_recovery_options",
            new_callable=AsyncMock,
        ) as mock_check:
            mock_check.return_value = [MagicMock(text='{"status":"ok"}')]
            await handle_self_recovery({})  # no action specified
            mock_check.assert_called_once()


# ============================================================================
# handle_check_recovery_options
# ============================================================================

class TestCheckRecoveryOptions:

    def _make_mock_server(self, coherence=0.8, risk=0.3, void_active=False, void_value=0.0):
        mock_server = MagicMock()
        mock_monitor = MagicMock()
        mock_monitor.state.coherence = coherence
        mock_monitor.state.void_active = void_active
        mock_monitor.state.V = void_value
        mock_monitor.get_metrics.return_value = {"mean_risk": risk}
        mock_server.get_or_create_monitor.return_value = mock_monitor
        return mock_server

    @pytest.mark.asyncio
    async def test_eligible_when_safe(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_check_recovery_options
        mock_server = self._make_mock_server()

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("test-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ):
            result = await handle_check_recovery_options({"_agent_uuid": "test-uuid"})
            text = json.loads(result[0].text)
            data = text.get("data", text)
            assert data["eligible"] is True
            assert len(data["blockers"]) == 0

    @pytest.mark.asyncio
    async def test_not_eligible_void_active(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_check_recovery_options
        mock_server = self._make_mock_server(void_active=True, void_value=0.9)

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("test-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ):
            result = await handle_check_recovery_options({"_agent_uuid": "test-uuid"})
            text = json.loads(result[0].text)
            data = text.get("data", text)
            assert data["eligible"] is False
            assert any(b["type"] == "void_active" for b in data["blockers"])

    @pytest.mark.asyncio
    async def test_not_eligible_high_risk(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_check_recovery_options
        mock_server = self._make_mock_server(risk=0.85)

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("test-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ):
            result = await handle_check_recovery_options({"_agent_uuid": "test-uuid"})
            text = json.loads(result[0].text)
            data = text.get("data", text)
            assert data["eligible"] is False
            assert any(b["type"] == "high_risk" for b in data["blockers"])

    @pytest.mark.asyncio
    async def test_unregistered_agent_error(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_check_recovery_options
        mock_error = MagicMock()
        mock_error.text = '{"error": "not registered"}'

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=(None, mock_error),
        ):
            result = await handle_check_recovery_options({})
            assert result[0] is mock_error


# ============================================================================
# handle_quick_resume
# ============================================================================

class TestQuickResume:

    def _make_mock_server(self, coherence=0.8, risk=0.2, void_active=False,
                          void_value=0.0, status="waiting_input"):
        mock_server = MagicMock()
        mock_monitor = MagicMock()
        mock_monitor.state.coherence = coherence
        mock_monitor.state.void_active = void_active
        mock_monitor.state.V = void_value
        mock_monitor.get_metrics.return_value = {"mean_risk": risk}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        mock_meta = MagicMock()
        mock_meta.status = status
        mock_meta.paused_at = None
        metadata = MagicMock()
        metadata.get = lambda k, d=None: mock_meta if k == "test-uuid" else d
        metadata.__contains__ = lambda self, k: k == "test-uuid"
        mock_server.agent_metadata = metadata
        return mock_server

    @pytest.mark.asyncio
    async def test_quick_resume_safe_state(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_quick_resume
        mock_server = self._make_mock_server()

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("test-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.verify_agent_ownership",
            return_value=True,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.store_discovery_internal",
            new_callable=AsyncMock,
            create=True,
        ), patch(
            "src.agent_storage.update_agent",
            new_callable=AsyncMock,
        ), patch(
            "src.agent_storage.persist_runtime_state",
            new_callable=AsyncMock,
        ):
            result = await handle_quick_resume({"_agent_uuid": "test-uuid"})
            text = json.loads(result[0].text)
            data = text.get("data", text)
            assert data.get("success") is True or data.get("recovered") is True

    @pytest.mark.asyncio
    async def test_quick_resume_unsafe_state(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_quick_resume
        mock_server = self._make_mock_server(coherence=0.4, risk=0.6)

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("test-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.verify_agent_ownership",
            return_value=True,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ):
            result = await handle_quick_resume({"_agent_uuid": "test-uuid"})
            text = json.loads(result[0].text)
            # Should fail with safety error
            assert "error" in text or "NOT_SAFE" in str(text)

    @pytest.mark.asyncio
    async def test_quick_resume_ownership_denied(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_quick_resume

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("test-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.verify_agent_ownership",
            return_value=False,
        ):
            result = await handle_quick_resume({"_agent_uuid": "test-uuid"})
            text = json.loads(result[0].text)
            assert "error" in text or "AUTH" in str(text)

    @pytest.mark.asyncio
    async def test_quick_resume_invalid_status(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_quick_resume
        mock_server = self._make_mock_server(status="deleted")

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("test-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.verify_agent_ownership",
            return_value=True,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ):
            result = await handle_quick_resume({"_agent_uuid": "test-uuid"})
            text = json.loads(result[0].text)
            assert "error" in text or "Cannot" in str(text)

    # =====================================================================
    # Watcher P011 — persist_runtime_state must be called with the
    # paused_at/loop_detector clears + lifecycle event, and a persist
    # failure must NOT mutate in-memory state. Mirrors the fix-resume
    # pattern from operations.py:80-109 / resume.py 3a0de6b.
    # =====================================================================

    @pytest.mark.asyncio
    async def test_quick_resume_persists_runtime_state(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_quick_resume
        mock_server = self._make_mock_server()

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("test-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.verify_agent_ownership",
            return_value=True,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.store_discovery_internal",
            new_callable=AsyncMock,
            create=True,
        ), patch(
            "src.agent_storage.update_agent",
            new_callable=AsyncMock,
        ) as mock_update, patch(
            "src.agent_storage.persist_runtime_state",
            new_callable=AsyncMock,
        ) as mock_persist:
            result = await handle_quick_resume({
                "_agent_uuid": "test-uuid",
                "reason": "soak passed",
            })
            text = json.loads(result[0].text)
            data = text.get("data", text)
            assert data.get("success") is True or data.get("recovered") is True

            mock_update.assert_awaited_once_with("test-uuid", status="active")
            assert mock_persist.await_count == 2
            attempt_call = mock_persist.await_args_list[0]
            assert attempt_call.args[0] == "test-uuid"
            assert attempt_call.kwargs["recovery_attempt_at"]
            call = mock_persist.await_args_list[-1]
            assert call.args[0] == "test-uuid"
            assert call.kwargs["paused_at"] is None
            assert call.kwargs["loop_detected_at"] is None
            assert call.kwargs["loop_cooldown_until"] is None
            event = call.kwargs["append_lifecycle_event"]
            assert event["event"] == "quick_resumed"
            assert "soak passed" in event["reason"]
            assert "timestamp" in event

    @pytest.mark.asyncio
    async def test_quick_resume_persist_failure_returns_persist_failed(self):
        """If update_agent raises, handler returns PERSIST_FAILED and meta is
        NOT mutated to active — prevents in-memory/DB divergence."""
        from src.mcp_handlers.lifecycle.self_recovery import handle_quick_resume
        mock_server = self._make_mock_server(status="paused")
        meta = mock_server.agent_metadata.get("test-uuid")

        update_agent_mock = AsyncMock(side_effect=RuntimeError("db down"))

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("test-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.verify_agent_ownership",
            return_value=True,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.store_discovery_internal",
            new_callable=AsyncMock,
            create=True,
        ), patch(
            "src.agent_storage.update_agent",
            update_agent_mock,
        ), patch(
            "src.agent_storage.persist_runtime_state",
            new_callable=AsyncMock,
        ):
            result = await handle_quick_resume({"_agent_uuid": "test-uuid"})
            text = json.loads(result[0].text)
            assert "PERSIST_FAILED" in str(text)
            # Critical: in-memory status was NOT flipped to active, and
            # add_lifecycle_event was not called — proves no field above
            # the try block was hoisted into a pre-persist mutation.
            assert meta.status != "active"
            meta.add_lifecycle_event.assert_not_called()


# ============================================================================
# handle_operator_resume_agent
# ============================================================================

class TestOperatorResumeAgent:

    def _make_mock_server(self, caller_label="Operator", caller_tags=None,
                          target_coherence=0.6, target_risk=0.3,
                          target_void_active=False, target_void_value=0.0,
                          target_status="paused"):
        mock_server = MagicMock()

        # Caller metadata
        caller_meta = MagicMock()
        caller_meta.label = caller_label
        caller_meta.tags = caller_tags or []

        # Target metadata
        target_meta = MagicMock()
        target_meta.status = target_status
        target_meta.paused_at = None

        _metadata_store = {
            "caller-uuid": caller_meta,
            "target-uuid": target_meta,
        }
        metadata = MagicMock()
        metadata.get = lambda k, d=None: _metadata_store.get(k, d)
        metadata.__contains__ = lambda self, k: k in _metadata_store
        mock_server.agent_metadata = metadata

        # Target monitor
        mock_monitor = MagicMock()
        mock_monitor.state.coherence = target_coherence
        mock_monitor.state.void_active = target_void_active
        mock_monitor.state.V = target_void_value
        mock_monitor.get_metrics.return_value = {"mean_risk": target_risk}
        mock_server.get_or_create_monitor.return_value = mock_monitor

        return mock_server

    @pytest.mark.asyncio
    async def test_operator_can_resume(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        mock_server = self._make_mock_server()

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.store_discovery_internal",
            new_callable=AsyncMock,
            create=True,
        ), patch(
            "src.agent_storage.update_agent",
            new_callable=AsyncMock,
        ), patch(
            "src.agent_storage.persist_runtime_state",
            new_callable=AsyncMock,
        ):
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
                "target_agent_id": "target-uuid",
                "reason": "Agent stuck after timeout",
            })
            text = json.loads(result[0].text)
            data = text.get("data", text)
            assert data.get("success") is True

    @pytest.mark.asyncio
    async def test_non_operator_rejected(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        mock_server = self._make_mock_server(caller_label="Regular Agent")

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ):
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
                "target_agent_id": "target-uuid",
                "reason": "Trying to resume",
            })
            text = json.loads(result[0].text)
            assert "error" in text or "NOT_OPERATOR" in str(text)

    @pytest.mark.asyncio
    async def test_operator_tag_accepted(self):
        """Operator identified by tag instead of label."""
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        mock_server = self._make_mock_server(
            caller_label="Central",
            caller_tags=["operator", "admin"],
        )

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.store_discovery_internal",
            new_callable=AsyncMock,
            create=True,
        ), patch(
            "src.agent_storage.update_agent",
            new_callable=AsyncMock,
        ), patch(
            "src.agent_storage.persist_runtime_state",
            new_callable=AsyncMock,
        ):
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
                "target_agent_id": "target-uuid",
                "reason": "Recovering stuck agent",
            })
            text = json.loads(result[0].text)
            data = text.get("data", text)
            assert data.get("success") is True

    @pytest.mark.asyncio
    async def test_hard_limit_void_active(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        mock_server = self._make_mock_server(
            target_void_active=True,
            target_void_value=0.9,
        )

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ):
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
                "target_agent_id": "target-uuid",
                "reason": "Trying to force resume",
            })
            text = json.loads(result[0].text)
            assert "error" in text or "VOID" in str(text)

    @pytest.mark.asyncio
    async def test_hard_limit_extreme_risk(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        mock_server = self._make_mock_server(target_risk=0.85)

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ):
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
                "target_agent_id": "target-uuid",
                "reason": "Trying despite high risk",
            })
            text = json.loads(result[0].text)
            assert "error" in text or "RISK" in str(text)

    @pytest.mark.asyncio
    async def test_soft_limit_blocks_without_force(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        mock_server = self._make_mock_server(target_risk=0.65, target_coherence=0.35)

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ):
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
                "target_agent_id": "target-uuid",
                "reason": "Trying with elevated risk",
            })
            text = json.loads(result[0].text)
            assert "error" in text or "SOFT_SAFETY" in str(text)

    @pytest.mark.asyncio
    async def test_soft_limit_bypassed_with_force(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        mock_server = self._make_mock_server(target_risk=0.65, target_coherence=0.35)

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.store_discovery_internal",
            new_callable=AsyncMock,
            create=True,
        ), patch(
            "src.agent_storage.update_agent",
            new_callable=AsyncMock,
        ), patch(
            "src.agent_storage.persist_runtime_state",
            new_callable=AsyncMock,
        ):
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
                "target_agent_id": "target-uuid",
                "reason": "Force resuming despite soft limits",
                "force": True,
            })
            text = json.loads(result[0].text)
            data = text.get("data", text)
            assert data.get("success") is True
            assert data.get("force_used") is True

    # =====================================================================
    # Watcher P011 — persist_runtime_state must be called with the
    # paused_at/loop_detector clears + lifecycle event, and a persist
    # failure must NOT mutate in-memory state. Mirrors the fix-resume
    # pattern from operations.py:80-109 / resume.py 3a0de6b.
    # =====================================================================

    @pytest.mark.asyncio
    async def test_operator_resume_persists_runtime_state(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        mock_server = self._make_mock_server()

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.store_discovery_internal",
            new_callable=AsyncMock,
            create=True,
        ), patch(
            "src.agent_storage.update_agent",
            new_callable=AsyncMock,
        ) as mock_update, patch(
            "src.agent_storage.persist_runtime_state",
            new_callable=AsyncMock,
        ) as mock_persist:
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
                "target_agent_id": "target-uuid",
                "reason": "Stuck agent recovery",
            })
            text = json.loads(result[0].text)
            data = text.get("data", text)
            assert data.get("success") is True

            mock_update.assert_awaited_once_with("target-uuid", status="active")
            mock_persist.assert_awaited_once()
            # First positional arg is the agent UUID; the rest are kwargs.
            call = mock_persist.await_args
            assert call.args[0] == "target-uuid"
            assert call.kwargs["paused_at"] is None
            assert call.kwargs["loop_detected_at"] is None
            assert call.kwargs["loop_cooldown_until"] is None
            event = call.kwargs["append_lifecycle_event"]
            assert event["event"] == "operator_resumed"
            assert "caller-uuid" in event["reason"]
            assert "Stuck agent recovery" in event["reason"]
            assert "timestamp" in event

    @pytest.mark.asyncio
    async def test_operator_resume_persist_failure_returns_persist_failed(self):
        """If update_agent raises, handler returns PERSIST_FAILED and target_meta
        is NOT mutated to active — prevents in-memory/DB divergence."""
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        mock_server = self._make_mock_server(target_status="paused")
        target_meta = mock_server.agent_metadata.get("target-uuid")

        update_agent_mock = AsyncMock(side_effect=RuntimeError("db down"))

        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
            mock_server,
        ), patch(
            "src.mcp_handlers.lifecycle.self_recovery.store_discovery_internal",
            new_callable=AsyncMock,
            create=True,
        ), patch(
            "src.agent_storage.update_agent",
            update_agent_mock,
        ), patch(
            "src.agent_storage.persist_runtime_state",
            new_callable=AsyncMock,
        ):
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
                "target_agent_id": "target-uuid",
                "reason": "Stuck",
            })
            text = json.loads(result[0].text)
            assert "PERSIST_FAILED" in str(text)
            # Critical: in-memory status was NOT flipped to active, and
            # add_lifecycle_event was not called — proves no field above
            # the try block was hoisted into a pre-persist mutation.
            assert target_meta.status != "active"
            target_meta.add_lifecycle_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_target_agent_id(self):
        from src.mcp_handlers.lifecycle.self_recovery import handle_operator_resume_agent
        with patch(
            "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
            return_value=("caller-agent", None),
        ):
            result = await handle_operator_resume_agent({
                "_agent_uuid": "caller-uuid",
            })
            text = json.loads(result[0].text)
            assert "error" in text or "MISSING_TARGET" in str(text)
