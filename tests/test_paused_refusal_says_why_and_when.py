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
    assert "operator can resume" in recovery["other_exits"]
    # The ungated agent(action='resume') must never be advertised to agents.
    assert "agent(action='resume')" not in recovery["other_exits"]


def test_expiry_is_described_as_re_evaluation_not_release():
    paused_at = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    recovery = paused_refusal_recovery(_meta(paused_at=paused_at.isoformat()))
    assert "the pause lifts" in recovery["other_exits"]
    text = recovery["other_exits"]
    assert "on that same call" not in text
    # Every check-in after a gap longer than the arm interval re-arms gap
    # recovery, so a slow-cadence agent is never re-paused; the text says so.
    arm = int(GovernanceConfig.GAP_RECOVERY_ARM_SECONDS)
    assert f"more than {arm}s after the previous one cannot pause" in text
    assert "at a slower cadence it does not pause again" in text
    assert "about the third check-in" not in text


def _first_pause_after_expiry(gaps_seconds, monkeypatch):
    """Drive the monitor's gap suppression over post-expiry check-ins.

    Each entry is the gap before that check-in; every check-in carries a
    pause verdict. Returns the 1-based index of the first one left as a
    pause, or None. Arming mirrors UNITARESMonitor's elapsed-time check.
    """
    import src.governance_monitor as gm

    monkeypatch.setattr(gm.audit_logger, "log_attest_gap_suppressed", lambda **kw: None)
    monitor = SimpleNamespace(agent_id="a", _gap_recovery_cycles_remaining=0)
    for index, gap in enumerate(gaps_seconds, start=1):
        if gap > gm.config.GAP_RECOVERY_ARM_SECONDS:
            monitor._gap_recovery_cycles_remaining = gm.config.GAP_RECOVERY_CYCLES
        decision = gm.UNITARESMonitor._maybe_gap_suppress(
            monitor, {"action": "pause", "reason": "risk"}, gap, 0.8,
        )
        if decision["action"] == "pause":
            return index
    return None


def test_the_re_pause_rule_matches_the_monitor(monkeypatch):
    from config.governance_config import config

    arm = config.GAP_RECOVERY_ARM_SECONDS
    cycles = config.GAP_RECOVERY_CYCLES
    expiry_gap = 72 * 3600
    # Close cadence: the text says the pause returns on the `cycles`-th
    # check-in in a row within the arm interval, after the gapped one.
    close = [expiry_gap] + [arm / 2] * (cycles + 2)
    assert _first_pause_after_expiry(close, monkeypatch) == 1 + cycles
    # Slow cadence: every check-in re-arms, so nothing ever pauses again.
    slow = [expiry_gap] + [arm * 2] * 10
    assert _first_pause_after_expiry(slow, monkeypatch) is None
    # And the refusal names that same count.
    from src.mcp_handlers.support.pause_ttl import _post_gap_repause_text

    nth = {1: "first", 2: "second", 3: "third"}.get(cycles, f"{cycles}th")
    assert f"only on the {nth} check-in in a row" in _post_gap_repause_text()


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
    # The UUID, not meta.agent_id (often a display handle sessions are not
    # keyed by): dialectic sessions store paused_agent_id as the UUID.
    recovery = paused_refusal_recovery(_meta(agent_id="Claude_x_20260924"), "uuid-123")
    assert "dialectic(action='get', agent_id='uuid-123')" in recovery["other_exits"]
    assert "Claude_x_20260924" not in recovery["other_exits"]


def test_a_slow_event_write_still_matches_the_current_pause():
    """The in-memory event is stamped after a database round trip; a slow one
    (well over 5s) must still be recognised as this pause's reason."""
    paused_at = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    meta = _meta(
        paused_at=paused_at.isoformat(),
        lifecycle_events=[
            {"event": "paused", "reason": "older pause",
             "timestamp": (paused_at - timedelta(days=1)).isoformat()},
            {"event": "paused", "reason": "current pause",
             "timestamp": (paused_at + timedelta(seconds=30)).isoformat()},
        ],
    )
    assert paused_refusal_recovery(meta)["why"] == "current pause"


def test_a_huge_expiry_setting_does_not_crash_the_refusal(set_governance_config):
    """_pause_is_stale tolerates it; the refusal builder must too, or every
    paused agent's check-in fails with an unhandled OverflowError.

    Patched through the reload-safe fixture: the module-level GovernanceConfig
    import goes stale once another test reloads config.governance_config, and
    patching it then leaves the live class (which pause_ttl reads) untouched."""
    set_governance_config("PAUSE_AUTO_EXPIRE_SECONDS", 999999999999)
    recovery = paused_refusal_recovery(
        _meta(paused_at=datetime(2026, 9, 21, tzinfo=timezone.utc).isoformat())
    )
    assert "expires_at" not in recovery
    assert "No re-evaluation time is recorded" in recovery["other_exits"]
