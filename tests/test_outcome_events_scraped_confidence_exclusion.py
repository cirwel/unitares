"""Server-scraped confidences persist but never train calibration.

Third exclusion class alongside the synthetic-fixture guard (#770) and the
Phase-5 shadow guard (#1320), and it exists for the same reason: a row that
cannot carry a real prediction must not be allowed to poison the global
tactical/strategic channels.

`registry` (a prediction registered before the work and consumed by id) and
`argument` (the caller stated it on this call) are the caller's own number.
`prev_confidence_fallback` and `audit_trail_fallback` are not — the server
scrapes the agent's most recent confidence from anywhere, possibly a different
task hours earlier, and pairs it with THIS outcome. Calibration asks whether
stated confidence predicts outcomes; a number the agent never stated about this
work cannot answer that in either direction.

Measured 2026-07-31 over the clean epoch (since the 2026-07-26 reset):

    prediction_source          n    distinct conf   agents
    prev_confidence_fallback  155        4            16
    audit_trail_fallback       87        4            10
    registry                   15        6             5
    argument                    3        2             2

Four distinct values across 155 rows from 16 different agents is not a
confidence distribution, it is a handful of sticky per-agent defaults. See
#1321 (and #1345 for the prediction-protocol question this defers to).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline


def _mock_db(latest_confidence=None):
    db = MagicMock()
    db.get_latest_eisv_by_agent_id = AsyncMock(return_value=None)
    db.get_latest_confidence_before = AsyncMock(return_value=latest_confidence)
    db.record_outcome_event = AsyncMock(return_value="outcome-id")
    return db


async def _run(args_extra, *, latest_confidence=None, monitor=None, env=None):
    db = _mock_db(latest_confidence)
    checker = MagicMock()
    seq = MagicMock()
    args = {
        "agent_id": "agent-1",
        "outcome_type": "test_passed",             # hard-exogenous -> tactical channel
        "outcome_score": 1.0,
        "is_bad": False,
        "verification_source": "external_signal",  # evidence_weight 1.0, clears the 0.65 gate
        "detail": {"kind": "test", "tool": "python", "exit_code": 0},
        **args_extra,
    }
    patches = [
        patch("src.db.get_db", return_value=db),
        patch("src.calibration.calibration_checker", checker),
        patch("src.sequential_calibration.sequential_calibration_tracker", seq),
    ]
    if monitor is not None:
        server = MagicMock()
        server.monitors = {"agent-1": monitor}
        patches.append(
            patch("src.mcp_handlers.observability.outcome_events.mcp_server", server)
        )
    if env is not None:
        patches.append(patch.dict("os.environ", env, clear=False))
    for p in patches:
        p.start()
    try:
        await _record_outcome_event_inline(args)
    finally:
        for p in reversed(patches):
            p.stop()
    persisted = db.record_outcome_event.await_args.kwargs["detail"]
    return persisted, checker, seq, db


@pytest.mark.asyncio
async def test_audit_trail_fallback_persists_but_does_not_train():
    """No caller confidence -> server reads it from the audit trail -> excluded."""
    persisted, checker, seq, db = await _run({}, latest_confidence=0.6)

    db.record_outcome_event.assert_awaited_once()          # row still persists
    assert persisted["prediction_source"] == "audit_trail_fallback"
    assert persisted["reported_confidence"] == 0.6         # chain still resolves it
    assert persisted["calibration_excluded"] is True
    assert persisted["eprocess_eligible"] is False
    checker.record_prediction.assert_not_called()
    checker.record_tactical_decision.assert_not_called()
    seq.record_exogenous_tactical_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_prev_confidence_fallback_persists_but_does_not_train():
    """No caller confidence -> server reads the monitor's last one -> excluded."""
    monitor = MagicMock()
    monitor._prev_confidence = 0.7
    persisted, checker, seq, db = await _run({}, monitor=monitor)

    db.record_outcome_event.assert_awaited_once()
    assert persisted["prediction_source"] == "prev_confidence_fallback"
    assert persisted["reported_confidence"] == 0.7
    assert persisted["calibration_excluded"] is True
    checker.record_prediction.assert_not_called()
    seq.record_exogenous_tactical_outcome.assert_not_called()


@pytest.mark.asyncio
async def test_caller_supplied_confidence_still_trains():
    """The control. An explicitly-passed confidence IS the caller's own number."""
    persisted, checker, seq, _ = await _run({"confidence": 0.9})

    assert persisted["prediction_source"] == "argument"
    assert persisted["calibration_excluded"] is False
    checker.record_prediction.assert_called_once()
    checker.record_tactical_decision.assert_called_once()


@pytest.mark.asyncio
async def test_missing_prediction_with_caller_argument_still_trains():
    """The subtle one, and why this gate keys on prediction_SOURCE.

    When a `prediction_id` is passed and the agent HAS a prediction registry
    that does not contain it, `prediction_binding` is pinned to
    "missing_prediction", and the later `if prediction_binding == "no_binding"`
    guards stop it being overwritten — so the BINDING reads missing while the
    caller did supply a confidence by argument. Keying the exclusion on the
    binding label would wrongly drop this row; keying it on `prediction_source`
    (where the number actually came from) keeps it.

    The registry must exist and be empty for that branch to be reachable. With
    no monitor at all the branch is skipped entirely and the binding lands on
    "argument_fallback" instead — which is why this wires one up explicitly.
    """
    monitor = MagicMock()
    monitor._open_predictions = {}            # real dict: present, but empty
    monitor._prediction_ttl_seconds = 3600.0
    persisted, checker, _, _ = await _run(
        {"confidence": 0.8, "prediction_id": "no-such-prediction"},
        monitor=monitor,
    )

    assert persisted["prediction_binding"] == "missing_prediction"
    assert persisted["prediction_source"] == "argument"
    assert persisted["calibration_excluded"] is False
    checker.record_prediction.assert_called_once()


@pytest.mark.asyncio
async def test_escape_hatch_restores_the_prior_behaviour():
    """UNITARES_CALIBRATION_ALLOW_SCRAPED_CONFIDENCE=1 -> trains again, no redeploy."""
    persisted, checker, seq, _ = await _run(
        {},
        latest_confidence=0.6,
        env={"UNITARES_CALIBRATION_ALLOW_SCRAPED_CONFIDENCE": "1"},
    )

    assert persisted["prediction_source"] == "audit_trail_fallback"
    assert persisted["calibration_excluded"] is False
    checker.record_prediction.assert_called_once()
    seq.record_exogenous_tactical_outcome.assert_called_once()


@pytest.mark.asyncio
async def test_synthetic_fixture_exclusion_is_unaffected():
    """Regression lock: the pre-existing exclusion classes still fire on their own."""
    persisted, checker, _, _ = await _run(
        {"confidence": 0.9, "detail": {"synthetic_calibration_fixture": True}}
    )

    assert persisted["prediction_source"] == "argument"   # not the scraped path
    assert persisted["calibration_excluded"] is True      # excluded anyway
    checker.record_prediction.assert_not_called()
