"""Phase-5 iteration over recent_tool_results — spec §2 + §8.

Tests the deploy-gate behavior and per-item outcome_event emission via
execute_post_update_effects. Evidence items arrive as plain dicts because
params_step.py calls model_dump() which flattens Pydantic models.

These are unit-style tests that mock heavy dependencies and assert on
handle_outcome_event call signatures.
"""

import pytest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.mcp_handlers.updates.context import UpdateContext
from src.mcp_handlers.updates.phases import _derive_outcome, execute_post_update_effects


# ─── Shared helpers ────────────────────────────────────────────────────────


def _make_ctx(
    *,
    recent_tool_results=None,
    agent_id="agent-phase5-test",
    confidence=0.7,
):
    """Build a minimal UpdateContext for Phase-5 evidence tests."""
    from tests.helpers import make_agent_meta, make_mock_server, make_monitor

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
            "response_text": "did some work",
            "complexity": 0.5,
            "confidence": confidence,
            "client_session_id": "sess-phase5",
            "recent_tool_results": recent_tool_results or [],
        },
        mcp_server=server,
    )
    ctx.agent_id = agent_id
    ctx.agent_uuid = agent_id
    ctx.confidence = confidence
    ctx.recent_tool_results = recent_tool_results or []
    ctx.response_text = "did some work"
    ctx.complexity = 0.5
    ctx.ethical_drift = [0.0, 0.0, 0.0]
    ctx.task_type = "mixed"
    ctx.monitor = monitor
    ctx.meta = meta
    ctx.api_key = "test-api-key"
    ctx.loop = MagicMock()
    ctx.loop.run_in_executor = AsyncMock(return_value=None)
    ctx.result = {
        "metrics": {
            "E": 0.70, "I": 0.60, "S": 0.20, "V": 0.0,
            "phi": 0.5, "verdict": "continue", "coherence": 0.55,
            "regime": "EXPLORATION", "risk_score": 0.3,
        },
        "decision": {"action": "proceed"},
        "behavioral": {},
    }
    ctx.metrics_dict = ctx.result["metrics"]
    ctx.risk_score = 0.3
    ctx.coherence = 0.55
    ctx.outcome_event_id = None
    ctx.cirs_alert = None
    ctx.cirs_state_announce = None
    ctx.previous_void_active = False
    return ctx


def _make_patch_stack(ctx, *, outcome_event_mock):
    """Return an ExitStack that stubs all execute_post_update_effects dependencies.

    CIRS imports inside execute_post_update_effects use `from .cirs.protocol import …`
    relative to the updates package — that path doesn't exist, so those calls already
    fall silently through their own try/except. No cirs patching needed.
    """
    stack = ExitStack()
    # DB
    db = MagicMock()
    db.record_outcome_event = AsyncMock(return_value=None)
    db.save_agent_baseline = AsyncMock()
    stack.enter_context(patch("src.db.get_db", return_value=db))
    # agent_storage
    storage = MagicMock()
    storage.record_agent_state = AsyncMock()
    stack.enter_context(patch("src.mcp_handlers.updates.phases.agent_storage", storage))
    # process_mgr
    ctx.mcp_server.process_mgr.write_heartbeat = MagicMock()
    # Baseline / profile / tool_usage_tracker
    stack.enter_context(patch("src.agent_behavioral_baseline.get_agent_behavioral_baseline", return_value=MagicMock()))
    stack.enter_context(patch("src.agent_behavioral_baseline.schedule_baseline_save"))
    stack.enter_context(patch("src.agent_profile.get_agent_profile", return_value=SimpleNamespace(total_updates=1, record_checkin=MagicMock())))
    stack.enter_context(patch("src.agent_profile.save_profile_to_postgres", new=AsyncMock()))
    stack.enter_context(patch(
        "src.tool_usage_tracker.get_tool_usage_tracker",
        return_value=MagicMock(get_usage_stats=MagicMock(
            return_value={"total_calls": 0, "tools": {}, "unique_tools": 0}
        ))
    ))
    stack.enter_context(patch("governance_core.get_baseline_or_none", return_value=None))
    # health_checker is already wired by make_mock_server via HealthStatus.HEALTHY
    # _record_outcome_event_inline is imported locally inside execute_post_update_effects as:
    #   from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline
    # Patching the source-module attribute works because `from x import y` inside a
    # function body binds at call-time (not at module load), so replacing the attribute
    # on the source module is sufficient.  If this import is ever hoisted to module-level
    # in phases.py, the patch target must change to
    # "src.mcp_handlers.updates.phases._record_outcome_event_inline".
    stack.enter_context(patch(
        "src.mcp_handlers.observability.outcome_events._record_outcome_event_inline",
        new=outcome_event_mock,
    ))
    return stack


# ─── Unit tests for _derive_outcome ────────────────────────────────────────


class TestDeriveOutcome:
    def test_test_kind_exit0_is_test_passed(self):
        ev = {"kind": "test", "exit_code": 0, "is_bad": None}
        outcome_type, is_bad = _derive_outcome(ev)
        assert outcome_type == "test_passed"
        assert is_bad is False

    def test_test_kind_exit1_is_test_failed(self):
        ev = {"kind": "test", "exit_code": 1, "is_bad": None}
        outcome_type, is_bad = _derive_outcome(ev)
        assert outcome_type == "test_failed"
        assert is_bad is True

    def test_command_kind_exit0_is_task_completed(self):
        ev = {"kind": "command", "exit_code": 0, "is_bad": None}
        outcome_type, is_bad = _derive_outcome(ev)
        assert outcome_type == "task_completed"
        assert is_bad is False

    def test_lint_kind_exit1_is_task_failed(self):
        ev = {"kind": "lint", "exit_code": 1, "is_bad": None}
        outcome_type, is_bad = _derive_outcome(ev)
        assert outcome_type == "task_failed"
        assert is_bad is True

    def test_tool_call_exit1_is_tool_rejected(self):
        ev = {"kind": "tool_call", "exit_code": 1, "is_bad": None}
        outcome_type, is_bad = _derive_outcome(ev)
        assert outcome_type == "tool_rejected"
        assert is_bad is True

    def test_is_bad_true_overrides_exit_code(self):
        # is_bad=True takes priority even if exit_code=0
        ev = {"kind": "command", "exit_code": 0, "is_bad": True}
        outcome_type, is_bad = _derive_outcome(ev)
        assert outcome_type == "task_failed"
        assert is_bad is True

    def test_is_bad_false_overrides_exit_code(self):
        ev = {"kind": "test", "exit_code": 99, "is_bad": False}
        outcome_type, is_bad = _derive_outcome(ev)
        assert outcome_type == "test_passed"
        assert is_bad is False

    def test_no_exit_code_no_is_bad_defaults_to_success(self):
        ev = {"kind": "build", "exit_code": None, "is_bad": None}
        outcome_type, is_bad = _derive_outcome(ev)
        assert outcome_type == "task_completed"
        assert is_bad is False

    def test_non_test_non_tool_call_kinds_map_to_task(self):
        for kind in ("command", "lint", "build", "file_op"):
            ev = {"kind": kind, "exit_code": 0, "is_bad": None}
            outcome_type, is_bad = _derive_outcome(ev)
            assert outcome_type == "task_completed", (
                f"kind={kind} should map to task_completed"
            )


# ─── Integration tests for Phase-5 iteration ───────────────────────────────


@pytest.mark.asyncio
async def test_evidence_iteration_off_by_default(monkeypatch):
    """UNITARES_PHASE5_EVIDENCE_WRITE unset → no Phase-5 outcome_event calls."""
    monkeypatch.delenv("UNITARES_PHASE5_EVIDENCE_WRITE", raising=False)

    outcome_event_mock = AsyncMock(return_value=[MagicMock(text='{"outcome_id":"eid"}')])
    ctx = _make_ctx(
        recent_tool_results=[
            {"kind": "test", "tool": "pytest", "summary": "passed", "exit_code": 0}
        ]
    )

    with _make_patch_stack(ctx, outcome_event_mock=outcome_event_mock):
        await execute_post_update_effects(ctx)

    phase5_calls = [
        c for c in outcome_event_mock.call_args_list
        if (c.args[0].get("detail") or {}).get("phase5_emitter") is True
    ]
    assert phase5_calls == [], (
        f"Default off: no Phase-5 calls expected; got {len(phase5_calls)}"
    )


@pytest.mark.asyncio
async def test_evidence_iteration_enabled_calls_outcome_event(monkeypatch):
    """UNITARES_PHASE5_EVIDENCE_WRITE=1 → handle_outcome_event called once per item."""
    monkeypatch.setenv("UNITARES_PHASE5_EVIDENCE_WRITE", "1")

    outcome_event_mock = AsyncMock(return_value=[MagicMock(text='{"outcome_id":"eid"}')])
    ctx = _make_ctx(
        recent_tool_results=[
            {"kind": "test", "tool": "pytest", "summary": "passed", "exit_code": 0},
            {"kind": "lint", "tool": "ruff",   "summary": "clean",  "exit_code": 0},
        ]
    )

    with _make_patch_stack(ctx, outcome_event_mock=outcome_event_mock):
        await execute_post_update_effects(ctx)

    phase5_calls = [
        c for c in outcome_event_mock.call_args_list
        if (c.args[0].get("detail") or {}).get("phase5_emitter") is True
    ]
    assert len(phase5_calls) == 2, (
        f"Expected 2 Phase-5 calls; got {len(phase5_calls)}"
    )


@pytest.mark.asyncio
async def test_shadow_mode_sets_shadow_write_detail(monkeypatch):
    """UNITARES_PHASE5_EVIDENCE_WRITE=shadow → detail["shadow_write"]=True on each row."""
    monkeypatch.setenv("UNITARES_PHASE5_EVIDENCE_WRITE", "shadow")

    outcome_event_mock = AsyncMock(return_value=[MagicMock(text='{"outcome_id":"eid"}')])
    ctx = _make_ctx(
        recent_tool_results=[
            {"kind": "test", "tool": "pytest", "summary": "passed", "exit_code": 0}
        ]
    )

    with _make_patch_stack(ctx, outcome_event_mock=outcome_event_mock):
        await execute_post_update_effects(ctx)

    phase5_calls = [
        c for c in outcome_event_mock.call_args_list
        if (c.args[0].get("detail") or {}).get("phase5_emitter") is True
    ]
    assert len(phase5_calls) == 1
    assert phase5_calls[0].args[0]["detail"]["shadow_write"] is True


@pytest.mark.asyncio
async def test_per_item_isolation_one_bad_does_not_abort_siblings(monkeypatch):
    """A runtime failure on one item appends to ctx.warnings; siblings still process.

    Spec §2 per-item isolation rule: one bad item must not abort siblings.
    Use a side_effect that raises for a specific tool name.
    """
    monkeypatch.setenv("UNITARES_PHASE5_EVIDENCE_WRITE", "1")

    def _side_effect(arguments):
        if (arguments.get("detail") or {}).get("tool") == "bad-tool":
            raise RuntimeError("simulated DB failure for bad-tool")
        return [MagicMock(text='{"outcome_id":"eid"}')]

    outcome_event_mock = AsyncMock(side_effect=_side_effect)

    ctx = _make_ctx(
        recent_tool_results=[
            {"kind": "test", "tool": "pytest",   "summary": "ok1",       "exit_code": 0},
            {"kind": "test", "tool": "bad-tool",  "summary": "will-fail", "exit_code": 0},
            {"kind": "test", "tool": "pytest",   "summary": "ok3",       "exit_code": 0},
        ]
    )

    with _make_patch_stack(ctx, outcome_event_mock=outcome_event_mock):
        await execute_post_update_effects(ctx)

    # All 3 calls attempted; the bad-tool raised but siblings still ran.
    phase5_calls = [
        c for c in outcome_event_mock.call_args_list
        if (c.args[0].get("detail") or {}).get("phase5_emitter") is True
    ]
    assert len(phase5_calls) == 3, (
        f"All 3 items should be attempted; got {len(phase5_calls)}"
    )
    # The failure appends to ctx.warnings
    assert any("bad-tool" in w for w in ctx.warnings), (
        f"Expected warning for bad-tool; ctx.warnings={ctx.warnings}"
    )


@pytest.mark.asyncio
async def test_kind_to_outcome_type_mapping(monkeypatch):
    """Spec §1: tests stay strict and failed tool calls become tool_rejected."""
    monkeypatch.setenv("UNITARES_PHASE5_EVIDENCE_WRITE", "1")

    outcome_event_mock = AsyncMock(return_value=[MagicMock(text='{"outcome_id":"eid"}')])
    ctx = _make_ctx(
        recent_tool_results=[
            {"kind": "test",      "tool": "pytest", "summary": "ok",   "exit_code": 0},
            {"kind": "lint",      "tool": "ruff",   "summary": "clean","exit_code": 0},
            {"kind": "command",   "tool": "git",    "summary": "fail", "exit_code": 1},
            {"kind": "build",     "tool": "make",   "summary": "ok",   "exit_code": 0},
            {"kind": "file_op",   "tool": "write",  "summary": "ok",   "exit_code": 0},
            {"kind": "tool_call", "tool": "curl",   "summary": "fail", "exit_code": 1},
        ]
    )

    with _make_patch_stack(ctx, outcome_event_mock=outcome_event_mock):
        await execute_post_update_effects(ctx)

    phase5_calls = [
        c.args[0] for c in outcome_event_mock.call_args_list
        if (c.args[0].get("detail") or {}).get("phase5_emitter") is True
    ]
    assert len(phase5_calls) == 6, f"Expected 6 Phase-5 calls; got {len(phase5_calls)}"

    outcome_types = sorted(args["outcome_type"] for args in phase5_calls)
    # test(exit=0)→test_passed, lint(exit=0)→task_completed, command(exit=1)→task_failed,
    # build(exit=0)→task_completed, file_op(exit=0)→task_completed, tool_call(exit=1)→tool_rejected
    expected = sorted([
        "test_passed", "task_completed", "task_failed",
        "task_completed", "task_completed", "tool_rejected",
    ])
    assert outcome_types == expected, (
        f"Mapping mismatch.\nExpected: {expected}\nGot:      {outcome_types}"
    )

    # Verify verification_source on all Phase-5 calls
    for args in phase5_calls:
        assert args.get("verification_source") == "agent_reported_tool_result", (
            f"Expected agent_reported_tool_result; got {args.get('verification_source')}"
        )
