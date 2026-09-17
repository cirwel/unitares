"""The check-in reply must advertise the check-in's prediction, not an evidence row's.

Observed live 2026-09-16: a ``sync_state`` carrying two ``recent_tool_results``
returned a ``prediction_id`` that the same request had already bound to the
last evidence row through the exactly-once claim (#2246). The caller's later
``record_result(prediction_id=...)`` was refused as PREDICTION_REUSE_CONFLICT
naming the emitter's row, and the check-in's own prediction expired unread.

Mechanism: ``build_process_update_response_data`` surfaces
``monitor._last_prediction_id``; ``UNITARESMonitor.register_tactical_prediction``
set that pointer on every mint; the Phase-5 evidence emitter runs after the
check-in's mint and minted once per row. The fix mints evidence ids with
``advertise=False``. These tests bind the REAL monitor method onto the test
monitor so the pointer semantics under test are the shipped ones.
"""

from types import MethodType
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.governance_monitor import UNITARESMonitor
from src.mcp_handlers.updates.phases import execute_post_update_effects
from src.services.update_response_service import build_process_update_response_data
from tests.test_phases_phase5_evidence import _make_ctx, _make_patch_stack


def _bind_real_registry(monitor) -> None:
    """Give a mock monitor the shipped prediction registry and mint method."""
    monitor._open_predictions = {}
    monitor._prediction_ttl_seconds = 3600.0
    monitor._last_prediction_id = None
    monitor.register_tactical_prediction = MethodType(
        UNITARESMonitor.register_tactical_prediction, monitor
    )


def test_register_tactical_prediction_advertise_false_registers_without_moving_pointer():
    monitor = MagicMock()
    _bind_real_registry(monitor)

    advertised = monitor.register_tactical_prediction(0.8, decision_action="proceed")
    assert monitor._last_prediction_id == advertised

    silent = monitor.register_tactical_prediction(
        0.8, decision_action="proceed", advertise=False
    )

    assert silent != advertised
    assert silent in monitor._open_predictions, "a silent mint is still registered"
    assert monitor._open_predictions[silent]["confidence"] == 0.8
    assert monitor._last_prediction_id == advertised, (
        "advertise=False must leave the reply pointer where the check-in put it"
    )


def test_register_tactical_prediction_default_still_advertises():
    monitor = MagicMock()
    _bind_real_registry(monitor)

    first = monitor.register_tactical_prediction(0.6)
    second = monitor.register_tactical_prediction(0.7)

    assert monitor._last_prediction_id == second
    assert {first, second} <= set(monitor._open_predictions)


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_mode", ["shadow", "1"])
async def test_phase5_evidence_does_not_replace_the_reply_prediction(monkeypatch, evidence_mode):
    """Two evidence rows mint two ids; the reply still carries the check-in's own."""
    monkeypatch.setenv("UNITARES_PHASE5_EVIDENCE_WRITE", evidence_mode)

    outcome_event_mock = AsyncMock(return_value=[MagicMock(text='{"outcome_id":"eid"}')])
    ctx = _make_ctx(
        recent_tool_results=[
            {"kind": "command", "tool": "git", "summary": "clean tree", "exit_code": 0},
            {"kind": "tool_call", "tool": "github", "summary": "listed PRs", "exit_code": None},
        ],
        confidence=0.75,
    )
    monitor = ctx.monitor
    _bind_real_registry(monitor)

    # What monitor_calibration does inside process_update, before post-update
    # effects run: the check-in's own mint, advertised.
    checkin_pid = monitor.register_tactical_prediction(0.75, decision_action="proceed")
    assert monitor._last_prediction_id == checkin_pid

    with _make_patch_stack(ctx, outcome_event_mock=outcome_event_mock):
        await execute_post_update_effects(ctx)

    phase5_ids = [
        c.args[0].get("prediction_id")
        for c in outcome_event_mock.call_args_list
        if (c.args[0].get("detail") or {}).get("phase5_emitter") is True
    ]
    assert len(phase5_ids) == 2
    assert len(set(phase5_ids)) == 2, "each evidence row binds to its own prediction"
    assert checkin_pid not in phase5_ids, "an evidence row must not consume the check-in's id"
    assert set(phase5_ids) <= set(monitor._open_predictions), "evidence ids are registered"

    # The pointer the reply reads is untouched by the emitter...
    assert monitor._last_prediction_id == checkin_pid

    # ...so the reply advertises the id the caller can still grade.
    response = build_process_update_response_data(
        result={},
        agent_id=ctx.agent_id,
        identity_assurance={"tier": "strong"},
        monitor=monitor,
        ctx_warnings=ctx.warnings,
    )
    assert response["prediction_id"] == checkin_pid
    assert response["prediction_id"] not in phase5_ids


@pytest.mark.asyncio
async def test_phase5_evidence_without_checkin_prediction_advertises_nothing(monkeypatch):
    """No confidence-backed check-in mint means no reply prediction_id, even with evidence."""
    monkeypatch.setenv("UNITARES_PHASE5_EVIDENCE_WRITE", "shadow")

    outcome_event_mock = AsyncMock(return_value=[MagicMock(text='{"outcome_id":"eid"}')])
    ctx = _make_ctx(
        recent_tool_results=[
            {"kind": "test", "tool": "pytest", "summary": "passed", "exit_code": 0},
        ],
        confidence=0.5,
    )
    monitor = ctx.monitor
    _bind_real_registry(monitor)
    # monitor_calibration clears the pointer when no report created a prediction.
    monitor._last_prediction_id = None

    with _make_patch_stack(ctx, outcome_event_mock=outcome_event_mock):
        await execute_post_update_effects(ctx)

    phase5_ids = [
        c.args[0].get("prediction_id")
        for c in outcome_event_mock.call_args_list
        if (c.args[0].get("detail") or {}).get("phase5_emitter") is True
    ]
    assert len(phase5_ids) == 1 and phase5_ids[0] in monitor._open_predictions
    assert monitor._last_prediction_id is None

    response = build_process_update_response_data(
        result={},
        agent_id=ctx.agent_id,
        identity_assurance={"tier": "strong"},
        monitor=monitor,
    )
    assert "prediction_id" not in response
