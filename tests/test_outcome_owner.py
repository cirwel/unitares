"""Compatibility between the outcome owner and its legacy handler adapter."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src import outcomes
from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome_id", ["outcome-parity", None])
async def test_legacy_adapter_preserves_owner_acknowledgement_and_write(outcome_id):
    """Both entry points retain success/failure, grading and argument mutation."""
    db = SimpleNamespace(
        get_latest_eisv_by_agent_id=AsyncMock(return_value=None),
        record_outcome_event=AsyncMock(return_value=outcome_id),
    )
    submitted = {
        "agent_id": "outcome-owner-parity",
        "outcome_type": "test_passed",
        "confidence": 0.8,
        "client_session_id": "parity-session",
        "verification_source": "external_signal",
        "_trusted_ingestion": True,
        "detail": {
            "kind": "test",
            "tool": "pytest",
            "exit_code": 0,
            "synthetic_calibration_fixture": True,
        },
    }
    owner_arguments = deepcopy(submitted)
    adapter_arguments = deepcopy(submitted)
    with (
        patch("src.db.get_db", return_value=db),
        patch.object(outcomes, "_get_monitor_runtime", return_value=SimpleNamespace(monitors={})),
    ):
        owner_ack = await outcomes.record_outcome(owner_arguments)
        owner_write = deepcopy(db.record_outcome_event.await_args.kwargs)
        adapter_ack = await _record_outcome_event_inline(adapter_arguments)

    assert adapter_ack == owner_ack
    assert db.record_outcome_event.await_count == 2
    assert db.record_outcome_event.await_args.kwargs == owner_write
    assert adapter_arguments == owner_arguments
    assert "_trusted_ingestion" not in owner_arguments
    assert owner_write["detail"]["calibration_excluded"] is True
    if outcome_id is None:
        assert owner_ack["error_code"] == "DB_ERROR"
    else:
        assert owner_ack["outcome_id"] == outcome_id


@pytest.mark.asyncio
async def test_legacy_adapter_propagates_owner_exception():
    failure = RuntimeError("storage unavailable")
    with patch.object(outcomes, "record_outcome", side_effect=failure) as record:
        arguments = {"agent_id": "agent", "outcome_type": "test_passed"}
        with pytest.raises(RuntimeError) as caught:
            await _record_outcome_event_inline(arguments)
    assert caught.value is failure
    record.assert_awaited_once_with(arguments)
