"""Sentinel must post fleet findings to /api/findings each cycle."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_sentinel_side_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect log/state/session paths to tmp_path and stub live DB/evaluator
    and desktop notification side effects (#2942).

    Without this fixture, `run_cycle(client=None)` appends fixture findings to
    the operator's live `~/Library/Logs/unitares-sentinel.log`, queries the live
    `lease_plane_events` table via `_emit_forced_release_alarms`, shells out to
    the Phase B evaluator, and writes `.sentinel_state` / `last_phase_b_verdict.json`.
    """
    import agents.sentinel.agent as sentinel_mod
    import agents.sentinel.phase_b_promotion as phase_b_mod

    monkeypatch.setenv("SENTINEL_LOG_FILE", str(tmp_path / "sentinel.log"))
    monkeypatch.setattr(sentinel_mod, "LOG_FILE", tmp_path / "sentinel.log")
    monkeypatch.setattr(sentinel_mod, "STATE_FILE", tmp_path / "sentinel_state.json")
    monkeypatch.setattr(sentinel_mod, "SESSION_FILE", tmp_path / "sentinel_session.json")
    monkeypatch.setattr(sentinel_mod, "LEGACY_SESSION_FILE", tmp_path / "legacy_session")
    monkeypatch.setattr(sentinel_mod, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(
        sentinel_mod.SentinelAgent,
        "_emit_forced_release_alarms",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        sentinel_mod.SentinelAgent,
        "_emit_phase_b_transitions",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        phase_b_mod,
        "DEFAULT_STATE_PATH",
        tmp_path / "last_phase_b_verdict.json",
    )
    monkeypatch.setattr(
        sentinel_mod,
        "detect_phase_b_transitions",
        lambda *a, **kw: [],
    )
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)


@pytest.mark.asyncio
async def test_run_cycle_posts_findings_for_high_severity(tmp_path: Path):
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
    assert "3 agents drifting in lockstep" in (tmp_path / "sentinel.log").read_text()


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

    with patch("agents.sentinel.agent.post_finding") as mock_post:
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


def test_sentinel_log_file_respects_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """SENTINEL_LOG_FILE overrides the default ~/Library/Logs path at import time (#2942)."""
    import importlib.util
    import agents.sentinel.agent as sentinel_mod

    custom_log = tmp_path / "custom-sentinel.log"
    monkeypatch.setenv("SENTINEL_LOG_FILE", str(custom_log))

    spec = importlib.util.spec_from_file_location(
        "sentinel_agent_env_test", Path(sentinel_mod.__file__).resolve()
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.LOG_FILE == custom_log

