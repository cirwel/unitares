"""End-to-end: posting corroborated outcome_event rows populate tactical state.

The integration surface is heavy (mcp_server monitors, db pool, eisv snapshot
pipeline). To stay focused on the wiring change in Task 3, this test mocks the
upstream dependencies and asserts the call to calibration_checker carries the
expected signal_source.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

import src.calibration as calibration_module
from src.mcp_handlers.observability.outcome_events import handle_outcome_event


@pytest.fixture
def fresh_checker(tmp_path, monkeypatch):
    from src.calibration import CalibrationChecker
    fresh = CalibrationChecker(state_file=tmp_path / "calibration_state.json")
    # The handler does `from src.calibration import calibration_checker` locally,
    # so we must patch the source module.
    monkeypatch.setattr(calibration_module, "calibration_checker", fresh)
    return fresh


def _mock_db():
    """Return a db whose every awaited method resolves to a sensible stub.

    The handler awaits multiple db methods (get_latest_eisv_by_agent_id,
    record_outcome_event, get_latest_confidence_before, etc.). Rather than
    enumerate them, default every attribute access to an AsyncMock returning
    None — except record_outcome_event which must return a non-None outcome_id
    so the handler proceeds past its DB_ERROR check.
    """
    class AsyncDB:
        def __getattr__(self, name):
            if name == "record_outcome_event":
                return AsyncMock(return_value="outcome-id-1")
            return AsyncMock(return_value=None)
    return AsyncDB()


def _patch_handler_deps(monkeypatch, agent_id="test-agent"):
    """Patch the heavy upstream surface so we exercise only the calibration branch.

    `get_db` is imported locally inside the handler, so we patch it on `src.db`
    where it lives, not on the handler module.
    """
    from src.mcp_handlers.observability import outcome_events as oe_mod
    from src.mcp_handlers import context as ctx_mod
    import src.db as db_mod

    monkeypatch.setattr(db_mod, "get_db", _mock_db)
    monkeypatch.setattr(ctx_mod, "get_context_agent_id", lambda: agent_id)
    monkeypatch.setattr(ctx_mod, "get_context_client_session_id", lambda: None)

    mock_server = MagicMock()
    mock_server.monitors = {}
    monkeypatch.setattr(oe_mod, "mcp_server", mock_server)

    import src.services.runtime_queries as rq_mod
    monkeypatch.setattr(
        rq_mod, "_build_eisv_semantics",
        lambda *a, **kw: {"verdict": "proceed", "regime": "balanced"},
    )


@pytest.mark.asyncio
class TestOutcomeEventToTacticalChannel:
    async def test_claim_only_task_completed_does_not_populate_tasks_channel(self, fresh_checker, monkeypatch):
        _patch_handler_deps(monkeypatch)

        await handle_outcome_event({
            "outcome_type": "task_completed",
            "agent_id": "test-agent",
            "confidence": 0.8,
            "outcome_score": 1.0,
        })

        per_channel = fresh_checker.compute_tactical_metrics_per_channel()
        assert "tasks" not in per_channel

    async def test_tool_observed_task_completed_populates_tasks_channel(self, fresh_checker, monkeypatch):
        _patch_handler_deps(monkeypatch)

        await handle_outcome_event({
            "outcome_type": "task_completed",
            "agent_id": "test-agent",
            "confidence": 0.8,
            "outcome_score": 1.0,
            "detail": {
                "phase5_emitter": True,
                "kind": "command",
                "tool": "make",
                "exit_code": 0,
            },
        })

        per_channel = fresh_checker.compute_tactical_metrics_per_channel()
        assert "tasks" in per_channel, f"expected 'tasks' in {list(per_channel)}"
        assert sum(b.count for b in per_channel["tasks"].values()) == 1

    async def test_task_failed_populates_tasks_channel(self, fresh_checker, monkeypatch):
        _patch_handler_deps(monkeypatch)

        await handle_outcome_event({
            "outcome_type": "task_failed",
            "agent_id": "test-agent",
            "confidence": 0.6,
            "outcome_score": 0.0,
            "detail": {
                "phase5_emitter": True,
                "kind": "command",
                "tool": "make",
                "exit_code": 1,
            },
        })

        per_channel = fresh_checker.compute_tactical_metrics_per_channel()
        assert "tasks" in per_channel
        assert sum(b.count for b in per_channel["tasks"].values()) == 1

    async def test_test_passed_populates_tests_channel(self, fresh_checker, monkeypatch):
        _patch_handler_deps(monkeypatch)

        await handle_outcome_event({
            "outcome_type": "test_passed",
            "agent_id": "test-agent",
            "confidence": 0.9,
            "outcome_score": 1.0,
            "detail": {
                "phase5_emitter": True,
                "kind": "test",
                "tool": "pytest",
                "exit_code": 0,
            },
        })

        per_channel = fresh_checker.compute_tactical_metrics_per_channel()
        assert "tests" in per_channel
        assert sum(b.count for b in per_channel["tests"].values()) == 1
