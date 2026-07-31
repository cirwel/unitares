"""Sentinel must post fleet findings to /api/findings each cycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_run_cycle_posts_findings_for_high_severity():
    """High-severity fleet findings are POSTed to /api/findings with a stable fingerprint."""
    from agents.sentinel.agent import SentinelAgent

    agent = SentinelAgent.__new__(SentinelAgent)
    agent._cycle_count = 0
    agent._findings_total = 0
    agent._ws_connected = True
    agent.agent_uuid = "sentinel-test-uuid"
    agent.fleet = MagicMock()
    agent.fleet.analyze.return_value = [
        {
            "severity": "high",
            "type": "coordinated_degradation",
            "violation_class": "BEH",
            "summary": "3 agents drifting in lockstep",
        }
    ]
    agent.fleet.fleet_summary.return_value = {"active_agents": 3}

    with patch("agents.sentinel.agent.post_finding") as mock_post:
        await agent.run_cycle(client=None)

    assert mock_post.called
    # Select the fleet-finding call rather than trusting `call_args` to be it:
    # run_cycle emits forced-release alarms first, so relying on "the last call"
    # holds only by ordering luck and breaks when a new emitter lands after this one.
    fleet_calls = [c for c in mock_post.call_args_list
                   if c.kwargs.get("event_type") == "sentinel_finding"]
    assert len(fleet_calls) == 1, f"expected one fleet finding, got {len(fleet_calls)}"
    kwargs = fleet_calls[0].kwargs
    assert kwargs["severity"] == "high"
    assert "3 agents drifting in lockstep" in kwargs["message"]
    assert kwargs["agent_id"] == "sentinel-test-uuid"
    assert kwargs["fingerprint"]  # non-empty
    assert kwargs["extra"]["violation_class"] == "BEH"
    assert kwargs["extra"]["finding_type"] == "coordinated_degradation"


@pytest.mark.asyncio
async def test_run_cycle_does_not_write_findings_to_kg():
    """Findings go to the event stream only — never to the KG via CycleResult.notes.

    Regression for the redundant double-write pattern. Sentinel previously
    populated `notes` for high-severity findings, which the SDK routed to
    leave_note(). Those KG entries were ephemeral fleet snapshots with no
 archival value . Findings
    already reach the dashboard via post_finding(); the KG write was pure
    redundancy and noise.
    """
    from agents.sentinel.agent import SentinelAgent

    agent = SentinelAgent.__new__(SentinelAgent)
    agent._cycle_count = 0
    agent._findings_total = 0
    agent._ws_connected = True
    agent.agent_uuid = "sentinel-test-uuid"
    agent.fleet = MagicMock()
    agent.fleet.analyze.return_value = [
        {
            "severity": "high",
            "type": "verdict_shift",
            "violation_class": "ENT",
            "summary": "Pause rate 50% in last 10min (4/8)",
        }
    ]
    agent.fleet.fleet_summary.return_value = {"active_agents": 8}

    with patch("agents.sentinel.agent.post_finding"):
        result = await agent.run_cycle(client=None)

    # Result has no notes channel — KG never sees this
    assert result.notes is None


@pytest.mark.asyncio
async def test_run_cycle_does_not_post_self_observations():
    """Self-observations stay internal — they must not hit the event stream."""
    from agents.sentinel.agent import SentinelAgent

    agent = SentinelAgent.__new__(SentinelAgent)
    agent._cycle_count = 0
    agent._findings_total = 0
    agent._ws_connected = True
    agent.agent_uuid = "sentinel-test-uuid"
    agent.fleet = MagicMock()
    agent.fleet.analyze.return_value = [
        {"severity": "high", "type": "coherence_dip", "summary": "self only",
         "self_observation": True, "violation_class": ""}
    ]
    agent.fleet.fleet_summary.return_value = {"active_agents": 1}

    # `run_cycle` also calls `_emit_forced_release_alarms` (which in turn drives
    # phase-B transitions). That path queries the live governance DB and posts
    # under its own event types, so left unstubbed this test passes in CI (no DB,
    # no alarms) and fails on any machine with real lease-conflict state. Stub it
    # and assert on the channel this test is actually about.
    with patch("agents.sentinel.agent.post_finding") as mock_post, \
            patch.object(SentinelAgent, "_emit_forced_release_alarms",
                         new=AsyncMock(return_value=None)):
        await agent.run_cycle(client=None)

    # The claim is "no self-observation reaches the fleet-finding stream", not
    # "run_cycle posts nothing at all" — the latter breaks whenever run_cycle
    # grows an unrelated emitter, which is exactly what happened.
    fleet_finding_calls = [
        c for c in mock_post.call_args_list
        if c.kwargs.get("event_type") == "sentinel_finding"
    ]
    assert not fleet_finding_calls, (
        f"self-observation leaked to the event stream: {fleet_finding_calls}"
    )
