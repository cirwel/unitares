"""A paused agent's refusal says why it was paused and how a pause ends.

The refusal a paused agent actually reads said "Circuit breaker triggered due
to governance threshold violation": no reason, no end, and a threshold reading
cast as a violation. It now carries the reason recorded with this pause, says
writes are refused and not queued, does not present self_recovery as the way
out (a paused agent's risk is frozen at the reading that paused it), and names
the exits that end most pauses: the dialectic review, an operator, and
re-evaluation after expiry.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from config.governance_config import GovernanceConfig
from src.mcp_handlers.support.pause_ttl import paused_refusal_recovery


def _meta(**kw):
    base = dict(status="paused", paused_at=None, lifecycle_events=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_recovery_carries_the_recorded_reason_and_expiry():
    paused_at = datetime(2026, 9, 21, 0, 13, 55, tzinfo=timezone.utc)
    meta = _meta(
        paused_at=paused_at.isoformat(),
        lifecycle_events=[
            {"event": "resumed", "reason": "earlier",
             "timestamp": (paused_at - timedelta(hours=1)).isoformat()},
            {"event": "paused", "reason": "UNITARES high-risk verdict (risk_score=0.70)",
             "timestamp": (paused_at + timedelta(microseconds=900)).isoformat()},
        ],
    )
    recovery = paused_refusal_recovery(meta)
    assert recovery["why"] == "UNITARES high-risk verdict (risk_score=0.70)"
    expected = paused_at + timedelta(seconds=int(GovernanceConfig.PAUSE_AUTO_EXPIRE_SECONDS))
    assert recovery["expires_at"] == expected.isoformat()
    assert f"After {expected.isoformat()}" in recovery["other_exits"]


def test_recovery_says_writes_are_refused_and_names_the_exits():
    recovery = paused_refusal_recovery(_meta())
    assert "refused while paused and are not queued" in recovery["action"]
    assert "Dialectic moves are still accepted" in recovery["action"]
    assert "self-recovery eligibility" in recovery["action"]
    assert "self_recovery(action='check')" in recovery["action"]
    assert "cannot write the check-in that would lower it" in recovery["action"]
    assert "0.40" not in recovery["action"] and "0.65" not in recovery["action"]
    assert "dialectic(action='get', agent_id=" in recovery["other_exits"]
    assert "agent(action='resume')" in recovery["other_exits"]


def test_expiry_is_described_as_re_evaluation_not_release():
    paused_at = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    recovery = paused_refusal_recovery(_meta(paused_at=paused_at.isoformat()))
    assert "the pause lifts" in recovery["other_exits"]
    assert "pauses again on that same call" in recovery["other_exits"]


def test_an_earlier_pauses_reason_is_never_presented_as_current():
    paused_at = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    meta = _meta(
        paused_at=paused_at.isoformat(),
        lifecycle_events=[
            {"event": "paused", "reason": "an older pause",
             "timestamp": (paused_at - timedelta(days=2)).isoformat()},
        ],
    )
    assert paused_refusal_recovery(meta)["why"] == "No reason is recorded for this pause."


def test_missing_reason_and_bad_timestamp_degrade_honestly():
    recovery = paused_refusal_recovery(_meta(paused_at="not-a-date"))
    assert recovery["why"] == "No reason is recorded for this pause."
    assert "expires_at" not in recovery
    # Fail-closed: _pause_is_stale never expires such a pause, so the refusal
    # must not promise that it will.
    assert "No re-evaluation time is recorded" in recovery["other_exits"]
    assert "the pause lifts" not in recovery["other_exits"]


def test_no_refusal_calls_a_threshold_reading_a_violation():
    import inspect

    from src.mcp_handlers.support import agent_auth
    from src.mcp_handlers.updates import phases

    for module in (agent_auth, phases):
        source = inspect.getsource(module)
        assert "threshold violation" not in source, module.__name__
        assert "governance threshold exceeded" not in source, module.__name__


def test_the_dialectic_lookup_is_callable_as_written():
    """dialectic(action='get') errors without session_id or agent_id."""
    recovery = paused_refusal_recovery(_meta(agent_id="a-123"))
    assert "dialectic(action='get', agent_id='a-123')" in recovery["other_exits"]
