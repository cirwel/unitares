from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.updates.context import UpdateContext
from src.services.update_workflow_service import run_process_update_workflow
from tests.helpers import make_agent_meta, make_mock_server, make_monitor, parse_result


class _DummyLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_real_spine_harness(*, response_text, storage=None):
    import src.mcp_handlers.updates.enrichments  # noqa: F401

    agent_id = "agent-real-spine"
    session_key = "session-real-spine"
    meta = make_agent_meta(
        status="active",
        total_updates=5,
        purpose="testing",
        active_session_key=session_key,
    )
    monitor = make_monitor(
        coherence=0.52,
        coherence_history=[0.48, 0.52],
        E_history=[0.7, 0.71],
        V_history=[0.0, 0.0],
        mean_risk=0.3,
    )
    monitor.state.decision_history = []
    monitor.state.regime_history = []
    monitor.state.I_history = [0.6, 0.61]
    monitor.state.S_history = [0.2, 0.21]
    monitor._last_prediction_id = "pred-123"
    monitor._consecutive_high_drift = 0
    monitor.adaptive_governor = None

    server = make_mock_server(
        agent_metadata={agent_id: meta},
        monitors={agent_id: monitor},
    )

    db = MagicMock()
    db.load_agent_baseline = AsyncMock(return_value=None)
    db.record_outcome_event = AsyncMock(return_value="outcome-123")
    db.save_agent_baseline = AsyncMock()
    db.update_identity_metadata = AsyncMock()

    if storage is None:
        storage = MagicMock(
            get_agent=AsyncMock(return_value=SimpleNamespace(api_key="stored-key")),
            record_agent_state=AsyncMock(),
            create_agent=AsyncMock(),
            update_agent=AsyncMock(),
        )

    baseline = MagicMock()
    baseline.update = MagicMock()
    profile = SimpleNamespace(total_updates=1, record_checkin=MagicMock())
    tool_usage_tracker = MagicMock(
        get_usage_stats=MagicMock(return_value={"total_calls": 0, "tools": {}, "unique_tools": 0})
    )

    ctx = UpdateContext(
        arguments={
            "response_text": response_text,
            "complexity": 0.9,
            "confidence": 0.93,
            "task_type": "implementation",
            "response_mode": "full",
            "sensor_data": {
                "eisv": {"E": 0.81, "I": 0.62, "S": 0.18, "V": 0.04},
            },
            "client_session_id": session_key,
        },
        mcp_server=server,
    )

    return SimpleNamespace(
        agent_id=agent_id,
        session_key=session_key,
        ctx=ctx,
        meta=meta,
        server=server,
        db=db,
        storage=storage,
        monitor=monitor,
        baseline=baseline,
        profile=profile,
        tool_usage_tracker=tool_usage_tracker,
    )


def _patch_real_spine_edges(harness):
    stack = ExitStack()
    stack.enter_context(patch("src.mcp_handlers.context.get_context_agent_id", return_value=harness.agent_id))
    stack.enter_context(patch("src.mcp_handlers.context.get_context_session_key", return_value=harness.session_key))
    stack.enter_context(patch("src.mcp_handlers.context.get_session_resolution_source", return_value="ip_ua_fingerprint"))
    stack.enter_context(patch("src.mcp_handlers.context.get_trajectory_confidence", return_value=None))
    stack.enter_context(patch("src.mcp_handlers.identity.handlers.ensure_agent_persisted", new=AsyncMock(return_value=False)))
    stack.enter_context(patch("src.mcp_handlers.updates.phases.agent_storage", harness.storage))
    stack.enter_context(patch("src.db.get_db", return_value=harness.db))
    stack.enter_context(patch("src.tool_usage_tracker.get_tool_usage_tracker", return_value=harness.tool_usage_tracker))
    stack.enter_context(patch("src.agent_behavioral_baseline.ensure_baseline_loaded", new=AsyncMock(return_value=None)))
    stack.enter_context(patch("src.agent_behavioral_baseline.compute_anomaly_entropy", return_value=0.0))
    stack.enter_context(patch("src.agent_behavioral_baseline.get_agent_behavioral_baseline", return_value=harness.baseline))
    stack.enter_context(patch("src.agent_behavioral_baseline.schedule_baseline_save"))
    stack.enter_context(patch("src.agent_profile.get_agent_profile", return_value=harness.profile))
    stack.enter_context(patch("src.agent_profile.save_profile_to_postgres", new=AsyncMock()))
    return stack


@pytest.mark.asyncio
async def test_run_process_update_workflow_happy_path():
    ctx = SimpleNamespace(
        mcp_server=MagicMock(),
        agent_id="agent-123",
        agent_uuid="uuid-123",
        arguments={},
        identity_assurance={"tier": "strong"},
        result={"status": "ok"},
        meta=None,
        is_new_agent=False,
        key_was_generated=False,
        api_key_auto_retrieved=False,
        task_type="mixed",
        loop=AsyncMock(),
    )
    ctx.mcp_server.lock_manager.acquire_agent_lock_async.return_value = _DummyLock()
    ctx.mcp_server.monitors = {"agent-123": {"dummy": True}}

    with patch("src.mcp_handlers.updates.phases.resolve_identity_and_guards", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.handle_onboarding_and_resume", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.transform_inputs", return_value=None), \
         patch("src.mcp_handlers.updates.phases.execute_locked_update", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.prepare_unlocked_inputs", new=AsyncMock()), \
         patch("src.mcp_handlers.updates.phases.execute_post_update_effects", new=AsyncMock()), \
         patch("src.mcp_handlers.updates.pipeline.run_enrichment_pipeline", new=AsyncMock()), \
         patch("src.mcp_handlers.response_formatter.format_response", return_value={"status": "formatted"}), \
         patch("src.services.update_workflow_service.serialize_process_update_response", return_value=["done"]) as mock_serialize:
        result = await run_process_update_workflow(ctx)

    assert result == ["done"]
    mock_serialize.assert_called_once()
    assert ctx.monitor == {"dummy": True}


@pytest.mark.asyncio
async def test_run_process_update_workflow_returns_early_exit():
    ctx = SimpleNamespace(
        mcp_server=MagicMock(),
        arguments={},
    )
    early = ["stop"]
    with patch("src.mcp_handlers.updates.phases.resolve_identity_and_guards", new=AsyncMock(return_value=early)):
        result = await run_process_update_workflow(ctx)
    assert result == early


@pytest.mark.asyncio
async def test_run_process_update_workflow_timeout_uses_lock_error_category():
    class _TimeoutLockManager:
        def acquire_agent_lock_async(self, *args, **kwargs):
            raise TimeoutError("lock timeout")

    ctx = SimpleNamespace(
        mcp_server=MagicMock(lock_manager=_TimeoutLockManager()),
        agent_id="agent-123",
        agent_uuid="uuid-123",
        arguments={"client_session_id": "agent-123"},
        identity_assurance={"tier": "strong"},
        result={},
        meta=None,
        is_new_agent=False,
        key_was_generated=False,
        api_key_auto_retrieved=False,
        task_type="mixed",
        loop=AsyncMock(),
    )

    with patch("src.mcp_handlers.updates.phases.resolve_identity_and_guards", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.handle_onboarding_and_resume", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.transform_inputs", return_value=None), \
         patch("src.mcp_handlers.updates.phases.prepare_unlocked_inputs", new=AsyncMock()), \
         patch("src.lock_cleanup.cleanup_stale_state_locks", return_value={"cleaned": 0}):
        result = await run_process_update_workflow(ctx)

    data = parse_result(result)
    assert data["error_code"] == "LOCK_TIMEOUT"
    assert data["error_category"] == "system_error"
    assert data["lock_error"] is True


@pytest.mark.asyncio
async def test_enrichment_runs_outside_lock():
    """Enrichment pipeline must execute after the agent lock is released."""
    call_order = []

    class _OrderTrackingLock:
        async def __aenter__(self):
            call_order.append("lock_acquired")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            call_order.append("lock_released")
            return False

    ctx = SimpleNamespace(
        mcp_server=MagicMock(),
        agent_id="agent-123",
        agent_uuid="uuid-123",
        arguments={},
        identity_assurance={"tier": "strong"},
        result={"status": "ok"},
        meta=None,
        is_new_agent=False,
        key_was_generated=False,
        api_key_auto_retrieved=False,
        task_type="mixed",
        loop=AsyncMock(),
    )
    ctx.mcp_server.lock_manager.acquire_agent_lock_async.return_value = _OrderTrackingLock()
    ctx.mcp_server.monitors = {"agent-123": {"dummy": True}}

    async def fake_enrichment(c):
        call_order.append("enrichment_ran")

    with patch("src.mcp_handlers.updates.phases.resolve_identity_and_guards", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.handle_onboarding_and_resume", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.transform_inputs", return_value=None), \
         patch("src.mcp_handlers.updates.phases.execute_locked_update", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.prepare_unlocked_inputs", new=AsyncMock()), \
         patch("src.mcp_handlers.updates.phases.execute_post_update_effects", new=AsyncMock()), \
         patch("src.mcp_handlers.updates.pipeline.run_enrichment_pipeline", new=AsyncMock(side_effect=fake_enrichment)), \
         patch("src.mcp_handlers.response_formatter.format_response", return_value={"status": "formatted"}), \
         patch("src.services.update_workflow_service.serialize_process_update_response", return_value=["done"]):
        result = await run_process_update_workflow(ctx)

    assert result == ["done"]
    assert call_order.index("lock_released") < call_order.index("enrichment_ran"), \
        f"Enrichment ran inside the lock! Order: {call_order}"


@pytest.mark.asyncio
async def test_post_update_effects_run_outside_lock():
    """Post-update DB writes must execute after the agent lock is released."""
    call_order = []

    class _OrderTrackingLock:
        async def __aenter__(self):
            call_order.append("lock_acquired")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            call_order.append("lock_released")
            return False

    ctx = SimpleNamespace(
        mcp_server=MagicMock(),
        agent_id="agent-123",
        agent_uuid="uuid-123",
        arguments={},
        identity_assurance={"tier": "strong"},
        result={"status": "ok"},
        meta=None,
        is_new_agent=False,
        key_was_generated=False,
        api_key_auto_retrieved=False,
        task_type="mixed",
        loop=AsyncMock(),
    )
    ctx.mcp_server.lock_manager.acquire_agent_lock_async.return_value = _OrderTrackingLock()
    ctx.mcp_server.monitors = {"agent-123": {"dummy": True}}

    async def fake_post_effects(c):
        call_order.append("post_effects_ran")

    with patch("src.mcp_handlers.updates.phases.resolve_identity_and_guards", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.handle_onboarding_and_resume", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.transform_inputs", return_value=None), \
         patch("src.mcp_handlers.updates.phases.execute_locked_update", new=AsyncMock(return_value=None)), \
         patch("src.mcp_handlers.updates.phases.prepare_unlocked_inputs", new=AsyncMock()), \
         patch("src.mcp_handlers.updates.phases.execute_post_update_effects", new=AsyncMock(side_effect=fake_post_effects)), \
         patch("src.mcp_handlers.updates.pipeline.run_enrichment_pipeline", new=AsyncMock()), \
         patch("src.mcp_handlers.response_formatter.format_response", return_value={"status": "formatted"}), \
         patch("src.services.update_workflow_service.serialize_process_update_response", return_value=["done"]):
        result = await run_process_update_workflow(ctx)

    assert result == ["done"]
    assert call_order.index("lock_released") < call_order.index("post_effects_ran"), \
        f"Post-update effects ran inside the lock! Order: {call_order}"


@pytest.mark.asyncio
async def test_run_process_update_workflow_real_spine_with_edge_mocks():
    """Exercise the real workflow spine while mocking only storage/DB-style edges."""
    harness = _make_real_spine_harness(
        response_text="Implemented the fix by creating demo_probe.py outside tests/",
    )

    with _patch_real_spine_edges(harness):
        result = await run_process_update_workflow(harness.ctx)

    data = parse_result(result)
    call_kwargs = harness.server.process_update_authenticated_async.await_args.kwargs

    assert data["success"] is True
    assert data["agent_id"] == harness.agent_id
    assert data["identity_assurance"]["tier"] == "weak"
    assert data["prediction_id"] == "pred-123"
    assert data["outcome_event"]["outcome_id"] == "outcome-123"
    assert "tests/ directory" in data["warning"]
    assert call_kwargs["confidence"] == pytest.approx(0.55)
    assert call_kwargs["agent_state"]["sensor_eisv"] == {"E": 0.81, "I": 0.62, "S": 0.18, "V": 0.04}
    assert harness.ctx.monitor is harness.monitor
    assert harness.ctx.arguments["lite_response"] is True
    harness.storage.record_agent_state.assert_awaited_once()
    harness.db.record_outcome_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_update_records_s22_provenance_context():
    harness = _make_real_spine_harness(response_text="Recorded context.")
    harness.ctx.arguments.update({
        "harness": "codex-cli",
        "transport": "mcp-stdio",
        "model_provider": "openai",
        "model": "gpt-5.5",
        "tool_surface": ["terminal", "mcp:unitares"],
        "memory_context": "repo+kg",
        "comparison_key": "h5-bounded-task",
        "task_label": "H5 bounded task",
        "episode_id": "episode-1",
        "process_instance_id": "opaque-process",
    })

    with _patch_real_spine_edges(harness):
        await run_process_update_workflow(harness.ctx)

    call_kwargs = harness.server.process_update_authenticated_async.await_args.kwargs
    context = call_kwargs["agent_state"]["provenance_context"]
    assert context["schema"] == "s22.write_context.v1"
    assert context["context_source"] == "process_agent_update"
    assert context["harness_type"] == "codex-cli"
    assert context["transport"] == "mcp-stdio"
    assert context["model_provider"] == "openai"
    assert context["model"] == "gpt-5.5"
    assert context["tool_surface"] == ["terminal", "mcp:unitares"]
    assert context["memory_context"] == "repo+kg"
    assert context["comparison_key"] == "h5-bounded-task"
    assert context["task_label"] == "H5 bounded task"
    assert context["governance_mode"] == "explicit"
    assert context["session_resolution_source"] == "ip_ua_fingerprint"
    state_kwargs = harness.storage.record_agent_state.await_args.kwargs
    assert state_kwargs["provenance_context"] == context


@pytest.mark.asyncio
async def test_run_process_update_workflow_real_spine_retries_record_state_after_create():
    """Post-update state persistence should recover by creating the agent and retrying."""
    attempts = {"count": 0}

    async def _record_state_side_effect(**kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ValueError("Agent not found in database")
        return None

    storage = MagicMock(
        get_agent=AsyncMock(return_value=SimpleNamespace(api_key="stored-key")),
        record_agent_state=AsyncMock(side_effect=_record_state_side_effect),
        create_agent=AsyncMock(),
        update_agent=AsyncMock(),
    )
    harness = _make_real_spine_harness(
        response_text="Implemented a retry-safe persistence path.",
        storage=storage,
    )

    with _patch_real_spine_edges(harness):
        result = await run_process_update_workflow(harness.ctx)

    data = parse_result(result)

    assert data["success"] is True
    assert data["outcome_event"]["outcome_id"] == "outcome-123"
    harness.storage.create_agent.assert_awaited_once()
    assert harness.storage.record_agent_state.await_count == 2


@pytest.mark.asyncio
async def test_run_process_update_workflow_real_spine_refuses_archived_agent():
    """Archived agents are refused — no auto-resume path exists (Stage 3).

    Historically this branch auto-resumed on engagement. That behavior
    existed to rescue residents falsely orphan-archived. Now residents
    self-tag 'persistent' (PR #39), so the rescue is unnecessary and
    resurrection would mask real bugs. Archived agents must call
    self_recovery(action='quick') or onboard(force_new=true) explicitly.
    """
    harness = _make_real_spine_harness(
        response_text="Attempt to check in after archive — should be refused.",
    )
    harness.meta.status = "archived"
    harness.meta.archived_at = None
    harness.meta.notes = ""
    harness.meta.total_updates = 5

    metadata_cache = MagicMock(invalidate=AsyncMock())
    audit_logger = MagicMock()

    with _patch_real_spine_edges(harness), \
         patch("src.cache.get_metadata_cache", return_value=metadata_cache), \
         patch("src.audit_log.audit_logger", audit_logger):
        result = await run_process_update_workflow(harness.ctx)

    data = parse_result(result)

    assert data["success"] is False
    assert "archived" in data["error"].lower()
    assert "self_recovery" in data["recovery"]["action"]
    # Status was NOT flipped back to active; no persistence side effects.
    assert harness.meta.status == "archived"
    harness.storage.update_agent.assert_not_called()
    metadata_cache.invalidate.assert_not_called()
    harness.server.lock_manager.acquire_agent_lock_async.assert_not_called()


@pytest.mark.asyncio
async def test_run_process_update_workflow_real_spine_blocks_explicitly_archived_agent():
    """Explicitly archived agents should exit in Phase 2 before lock acquisition."""
    harness = _make_real_spine_harness(
        response_text="Trying to check in after an explicit archive.",
    )
    harness.meta.status = "archived"
    harness.meta.notes = "User requested archive after handoff"
    harness.meta.total_updates = 5

    metadata_cache = MagicMock(invalidate=AsyncMock())
    audit_logger = MagicMock()

    with _patch_real_spine_edges(harness), \
         patch("src.cache.get_metadata_cache", return_value=metadata_cache), \
         patch("src.audit_log.audit_logger", audit_logger):
        result = await run_process_update_workflow(harness.ctx)

    data = parse_result(result)

    assert data["success"] is False
    assert "archived" in data["error"] and "cannot" in data["error"]
    assert data["context"]["status"] == "archived"
    assert harness.meta.status == "archived"
    harness.server.lock_manager.acquire_agent_lock_async.assert_not_called()
    harness.server.process_update_authenticated_async.assert_not_awaited()
    harness.storage.update_agent.assert_not_called()
    metadata_cache.invalidate.assert_not_called()
