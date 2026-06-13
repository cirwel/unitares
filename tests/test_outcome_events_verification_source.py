"""Phase 1 (migration 038): verify the 4 direct callers of db.record_outcome_event
in src/mcp_handlers/updates/phases.py pass verification_source with the honest
classification per site. The MCP-tool path is covered by the existing schema
contract test (test_pydantic_schemas.py), Phase-5 evidence iteration by
test_phases_phase5_evidence.py, dialectic by test_dialectic_outcome_events.py.

Per-site classifications (using v1 enum):
  cirs_resonance        → server_observation (Sentinel telemetry-driven)
  task_completed        → agent_reported_tool_result (regex on response_text)
  task_failed           → agent_reported_tool_result (regex on response_text)
  trajectory_validated  → server_observation (computed from ctx.result)

Architect council 2026-05-19 flagged the existing 3-value enum as conflating
source / claimant / strength axes; v2 redesign is tracked separately in
project_outcome-verification-taxonomy-redesign. These tests pin the v1
classifications so the v2 work can be a deliberate motion, not silent drift.
"""
import json
import sys
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.helpers import make_agent_meta, make_mock_server, make_monitor
from src.db.mixins.tool_usage import ToolUsageMixin
from src.mcp_handlers.updates.context import UpdateContext


def _make_ctx(*, result_extra=None, response_text="did some work", complexity=0.5):
    """UpdateContext stub shaped to drive execute_post_update_effects through
    the 4 direct emitters. Mirrors _make_ctx in test_phases_phase5_evidence.py."""
    agent_id = "test-agent-uuid"
    meta = make_agent_meta(status="active", total_updates=3)
    monitor = make_monitor(
        coherence=0.55,
        coherence_history=[0.52, 0.55],
        E_history=[0.70, 0.71],
        V_history=[0.0, 0.0],
        mean_risk=0.3,
    )
    monitor.state.decision_history = []
    monitor.state.regime_history = []
    monitor.state.I_history = [0.6, 0.61]
    monitor.state.S_history = [0.2, 0.21]
    monitor._consecutive_high_drift = 0
    monitor.adaptive_governor = None
    server = make_mock_server(
        agent_metadata={agent_id: meta},
        monitors={agent_id: monitor},
    )
    ctx = UpdateContext(
        arguments={
            "response_text": response_text,
            "complexity": complexity,
            "confidence": 0.8,
            "client_session_id": "sess-1",
        },
        mcp_server=server,
    )
    ctx.agent_id = agent_id
    ctx.agent_uuid = agent_id
    ctx.confidence = 0.8
    ctx.response_text = response_text
    ctx.complexity = complexity
    ctx.ethical_drift = [0.0, 0.0, 0.0]
    ctx.task_type = "mixed"
    ctx.monitor = monitor
    ctx.meta = meta
    ctx.api_key = "test-api-key"
    ctx.loop = MagicMock()
    ctx.loop.run_in_executor = AsyncMock(return_value=None)
    base_metrics = {
        "E": 0.70, "I": 0.60, "S": 0.20, "V": 0.0,
        "phi": 0.5, "verdict": "continue", "coherence": 0.55,
        "regime": "EXPLORATION", "risk_score": 0.3,
    }
    result = {"metrics": dict(base_metrics), "decision": {"action": "proceed"}, "behavioral": {}}
    if result_extra:
        result.update(result_extra)
    ctx.result = result
    ctx.metrics_dict = result["metrics"]
    ctx.risk_score = 0.3
    ctx.coherence = 0.55
    ctx.recent_tool_results = []
    ctx.outcome_event_id = None
    ctx.cirs_alert = None
    ctx.cirs_state_announce = None
    ctx.previous_void_active = False
    return ctx


def _make_patch_stack(ctx, db):
    """Stub everything execute_post_update_effects touches except DB and ctx."""
    stack = ExitStack()
    stack.enter_context(patch("src.db.get_db", return_value=db))
    storage = MagicMock()
    storage.record_agent_state = AsyncMock()
    stack.enter_context(patch("src.mcp_handlers.updates.phases.agent_storage", storage))
    ctx.mcp_server.process_mgr.write_heartbeat = MagicMock()
    stack.enter_context(patch(
        "src.agent_behavioral_baseline.get_agent_behavioral_baseline",
        return_value=MagicMock(),
    ))
    stack.enter_context(patch("src.agent_behavioral_baseline.schedule_baseline_save"))
    stack.enter_context(patch(
        "src.agent_profile.get_agent_profile",
        return_value=SimpleNamespace(total_updates=1, record_checkin=MagicMock()),
    ))
    stack.enter_context(patch("src.agent_profile.save_profile_to_postgres", new=AsyncMock()))
    stack.enter_context(patch(
        "src.tool_usage_tracker.get_tool_usage_tracker",
        return_value=MagicMock(get_usage_stats=MagicMock(
            return_value={"total_calls": 0, "tools": {}, "unique_tools": 0}
        )),
    ))
    stack.enter_context(patch("governance_core.get_baseline_or_none", return_value=None))
    return stack


def _calls_by_type(record_mock):
    by_type = {}
    for call in record_mock.await_args_list:
        ot = call.kwargs.get("outcome_type")
        by_type.setdefault(ot, []).append(call.kwargs)
    return by_type


@pytest.mark.asyncio
async def test_cirs_resonance_emits_server_observation():
    """CIRS resonance is computed server-side from telemetry; not an agent claim."""
    db = MagicMock()
    db.record_outcome_event = AsyncMock(return_value="evt-cirs")
    db.save_agent_baseline = AsyncMock()
    ctx = _make_ctx(result_extra={"cirs": {
        "resonant": True, "oi": 0.5, "flips": 3,
        "trigger": "test", "response_tier": "warn",
    }})
    with _make_patch_stack(ctx, db):
        from src.mcp_handlers.updates.phases import execute_post_update_effects
        await execute_post_update_effects(ctx)
    by_type = _calls_by_type(db.record_outcome_event)
    assert "cirs_resonance" in by_type, by_type
    assert by_type["cirs_resonance"][0].get("verification_source") == "server_observation"


@pytest.mark.asyncio
async def test_task_completed_auto_checkin_emits_agent_reported():
    """Regex inference over agent's own response_text — closest v1-enum match
    is agent_reported_tool_result. Architect council flagged this as zero agent
    agency in the claim; v2 redesign would split into a separate value."""
    db = MagicMock()
    db.record_outcome_event = AsyncMock(return_value="evt-task")
    db.save_agent_baseline = AsyncMock()
    ctx = _make_ctx(response_text="I completed the task and shipped the PR.")
    with _make_patch_stack(ctx, db):
        from src.mcp_handlers.updates.phases import execute_post_update_effects
        await execute_post_update_effects(ctx)
    by_type = _calls_by_type(db.record_outcome_event)
    assert "task_completed" in by_type, by_type
    assert by_type["task_completed"][0].get("verification_source") == "agent_reported_tool_result"


@pytest.mark.asyncio
async def test_task_failed_auto_checkin_emits_agent_reported():
    """Same regex mechanism as task_completed, just on failure keywords."""
    db = MagicMock()
    db.record_outcome_event = AsyncMock(return_value="evt-fail")
    db.save_agent_baseline = AsyncMock()
    ctx = _make_ctx(response_text="The deploy failed and the build is broken.")
    with _make_patch_stack(ctx, db):
        from src.mcp_handlers.updates.phases import execute_post_update_effects
        await execute_post_update_effects(ctx)
    by_type = _calls_by_type(db.record_outcome_event)
    assert "task_failed" in by_type, by_type
    assert by_type["task_failed"][0].get("verification_source") == "agent_reported_tool_result"


@pytest.mark.asyncio
async def test_trajectory_validated_emits_server_observation():
    """Quality is computed server-side from ctx.result['trajectory_validation'].
    The agent doesn't see or assert the quality value; it's substrate-derived."""
    db = MagicMock()
    db.record_outcome_event = AsyncMock(return_value="evt-traj")
    db.save_agent_baseline = AsyncMock()
    ctx = _make_ctx(result_extra={"trajectory_validation": {
        "quality": 0.85,
        "prev_verdict": "allow",
        "prev_norm": 0.1,
        "current_norm": 0.12,
        "norm_delta": 0.02,
    }})
    with _make_patch_stack(ctx, db):
        from src.mcp_handlers.updates.phases import execute_post_update_effects
        await execute_post_update_effects(ctx)
    by_type = _calls_by_type(db.record_outcome_event)
    assert "trajectory_validated" in by_type, by_type
    assert by_type["trajectory_validated"][0].get("verification_source") == "server_observation"


def test_mixin_record_outcome_event_accepts_verification_source():
    """Mixin signature exposes the parameter; INSERT SQL includes the column.
    Pinning here so a later refactor doesn't silently drop the column from
    the INSERT statement — the failure mode that motivated Phase 1."""
    import inspect
    from src.db.mixins.tool_usage import ToolUsageMixin
    sig = inspect.signature(ToolUsageMixin.record_outcome_event)
    assert "verification_source" in sig.parameters
    src = inspect.getsource(ToolUsageMixin.record_outcome_event)
    assert "INSERT INTO audit.outcome_events" in src
    insert_start = src.index("INSERT INTO audit.outcome_events")
    insert_end = src.index("RETURNING outcome_id")
    insert_block = src[insert_start:insert_end]
    assert "verification_source" in insert_block, (
        "verification_source must appear in the INSERT column list, not just the docstring"
    )


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeOutcomeConn:
    def __init__(self, effects):
        self.effects = list(effects)
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        effect = self.effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


class _OutcomeMixinHarness(ToolUsageMixin):
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


@pytest.mark.asyncio
async def test_mixin_retries_legacy_insert_when_verification_source_column_missing():
    conn = _FakeOutcomeConn([
        Exception('column "verification_source" of relation "outcome_events" does not exist'),
        "legacy-outcome-id",
    ])
    db = _OutcomeMixinHarness(conn)

    result = await db.record_outcome_event(
        agent_id="agent-1",
        outcome_type="task_completed",
        is_bad=False,
        verification_source="agent_reported_tool_result",
    )

    assert result == "legacy-outcome-id"
    assert len(conn.calls) == 2
    assert "verification_source" in conn.calls[0][0]
    assert "verification_source" not in conn.calls[1][0]
    legacy_detail = json.loads(conn.calls[1][1][13])
    assert legacy_detail["verification_source"] == "agent_reported_tool_result"


@pytest.mark.asyncio
async def test_mixin_runs_partition_maintenance_once_for_missing_outcome_partition():
    conn = _FakeOutcomeConn([
        Exception('no partition of relation "outcome_events" found for row'),
        '{"outcome_events_current": "Created partition outcome_events_2026_06"}',
        "partition-retry-outcome-id",
    ])
    db = _OutcomeMixinHarness(conn)

    result = await db.record_outcome_event(
        agent_id="agent-1",
        outcome_type="task_completed",
        is_bad=False,
        verification_source="agent_reported_tool_result",
    )

    assert result == "partition-retry-outcome-id"
    assert len(conn.calls) == 3
    assert "verification_source" in conn.calls[0][0]
    assert "SELECT audit.partition_maintenance()" == conn.calls[1][0]
    assert "verification_source" in conn.calls[2][0]


@pytest.mark.asyncio
async def test_mixin_stamps_corroboration_metadata_into_detail():
    conn = _FakeOutcomeConn(["outcome-id"])
    db = _OutcomeMixinHarness(conn)

    result = await db.record_outcome_event(
        agent_id="agent-1",
        outcome_type="task_completed",
        is_bad=False,
        detail={"summary": "I completed the task."},
        verification_source="agent_reported_tool_result",
    )

    assert result == "outcome-id"
    detail = json.loads(conn.calls[0][1][13])
    assert detail["corroboration_grade"] == "claim_only"
    assert detail["evidence_weight"] == 0.10
    assert detail["claim_risk"] == "high"


@pytest.mark.asyncio
async def test_inline_outcome_response_echoes_corroboration_metadata():
    from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline

    db = MagicMock()
    db.get_latest_eisv_by_agent_id = AsyncMock(return_value=None)
    db.get_latest_confidence_before = AsyncMock(return_value=None)
    db.record_outcome_event = AsyncMock(return_value="outcome-id")

    with patch("src.db.get_db", return_value=db):
        payload = await _record_outcome_event_inline({
            "agent_id": "agent-1",
            "outcome_type": "task_completed",
            "detail": {"summary": "Finished the work."},
            "verification_source": "agent_reported_tool_result",
        })

    assert payload["corroboration_grade"] == "claim_only"
    assert payload["evidence_weight"] == 0.10
    assert payload["claim_risk"] == "high"
    persisted_detail = db.record_outcome_event.await_args.kwargs["detail"]
    assert persisted_detail["corroboration_grade"] == "claim_only"
    assert persisted_detail["eprocess_eligible"] is False
    assert persisted_detail["hard_exogenous"] is False
