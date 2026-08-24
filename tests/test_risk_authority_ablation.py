"""Three-arm synthetic restart ablation for the risk-authority boundary."""

from __future__ import annotations

import json

import pytest

import src._imports as project_imports
import src.agent_monitor_state as monitor_state
from src.governance_monitor import UNITARESMonitor
from src.mcp_handlers.lifecycle.recovery_policy import read_risk_authority


@pytest.mark.parametrize(
    (
        "agent_id",
        "resolved_risk",
        "resolved_verdict",
        "phi_risk",
        "expected_headline_risk",
        "expected_source",
        "expected_recovery_reading",
    ),
    (
        pytest.param(
            "resolved_low_phi_high",
            0.10,
            "safe",
            0.90,
            0.10,
            "resolved",
            0.10,
            id="resolved-low-phi-high",
        ),
        pytest.param(
            "resolved_high_phi_low",
            0.90,
            "high-risk",
            0.10,
            0.90,
            "resolved",
            0.90,
            id="resolved-high-phi-low",
        ),
        pytest.param(
            "phi_only",
            None,
            None,
            0.90,
            0.90,
            "phi_history",
            None,
            id="phi-only-yields-no-recovery-authority",
        ),
    ),
)
def test_risk_authority_survives_restart(
    tmp_path,
    monkeypatch,
    agent_id,
    resolved_risk,
    resolved_verdict,
    phi_risk,
    expected_headline_risk,
    expected_source,
    expected_recovery_reading,
):
    """Vary decision authority and Φ telemetry independently through a restart."""

    monkeypatch.setattr(project_imports, "ensure_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(monitor_state, "project_root", str(tmp_path))
    (tmp_path / "data" / "agents").mkdir(parents=True)

    monitor = UNITARESMonitor(agent_id=agent_id, load_state=False)
    monitor.state.update_count = 10
    monitor.state.risk_history = [phi_risk] * 10
    monitor._last_resolved_risk = resolved_risk
    monitor._last_resolved_verdict = resolved_verdict

    monitor_state.save_monitor_state(agent_id, monitor)
    state_path = tmp_path / "data" / "agents" / f"{agent_id}_state.json"
    saved = json.loads(state_path.read_text(encoding="utf-8"))

    restored = UNITARESMonitor(agent_id=agent_id)
    metrics = restored.get_metrics(include_state=False)

    assert metrics["risk_score"] == pytest.approx(expected_headline_risk)
    assert metrics["risk_score_source"] == expected_source
    assert metrics["phi_risk_current"] == pytest.approx(phi_risk)
    authority = read_risk_authority(metrics)
    if expected_recovery_reading is None:
        # The Φ-only arm: no scalar is produced for a recovery gate to read.
        # Every gate must branch on this and refuse, which is what
        # test_no_authority_blocks_self_recovery below pins end to end.
        assert authority.is_lost
        assert authority.risk is None
    else:
        assert authority.risk == pytest.approx(expected_recovery_reading)

    if resolved_risk is None:
        assert "resolved_risk" not in saved
        assert "resolved_verdict" not in saved
        assert restored._last_resolved_risk is None
        assert restored._last_resolved_verdict is None
    else:
        assert saved["resolved_risk"] == pytest.approx(resolved_risk)
        assert saved["resolved_verdict"] == resolved_verdict
        assert restored._last_resolved_risk == pytest.approx(resolved_risk)
        assert restored._last_resolved_verdict == resolved_verdict
        assert metrics["verdict"] == resolved_verdict
        assert metrics["verdict_resolution_source"] == "resolved"


def _gate_server(source: str, risk: float = 0.91):
    """A paused monitor whose Φ risk is high, varying only the source label."""
    from unittest.mock import MagicMock

    server = MagicMock()
    monitor = MagicMock()
    monitor.state.coherence = 0.30
    monitor.state.void_active = False
    monitor.state.V = 0.0
    monitor.get_metrics.return_value = {
        "risk_score": risk,
        "risk_score_source": source,
        "current_risk": risk,
        "mean_risk": risk,
        "coherence_source": "legacy_tanh_v",
        "coherence_role": "ode_control_feedback",
    }
    server.get_or_create_monitor.return_value = monitor
    return server


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ("resolved", "phi_history"))
async def test_no_authority_blocks_self_recovery(source):
    """Neither arm may be eligible, and neither for a fabricated reason.

    ``resolved`` blocks on the measured 0.91. ``phi_history`` has no reading at
    all, and must block on that rather than clearing the 0.65 limit with a
    stand-in midpoint.
    """
    import json
    from unittest.mock import patch

    from src.mcp_handlers.lifecycle.self_recovery import (
        handle_check_recovery_options,
    )

    with patch(
        "src.mcp_handlers.lifecycle.self_recovery.require_registered_agent",
        return_value=("test-agent", None),
    ), patch(
        "src.mcp_handlers.lifecycle.self_recovery.mcp_server",
        _gate_server(source),
    ):
        result = await handle_check_recovery_options({"_agent_uuid": "u"})

    payload = json.loads(result[0].text)
    data = payload.get("data", payload)
    assert data["eligible"] is False
    blocker_types = {b["type"] for b in data["blockers"]}
    if source == "resolved":
        assert blocker_types == {"high_risk"}
        assert data["metrics"]["risk_score"] == pytest.approx(0.91)
        assert data["margin"]["margin"] == "critical"
    else:
        assert blocker_types == {"no_risk_authority"}
        assert data["metrics"]["risk_score"] is None
        assert data["margin"]["margin"] == "unknown"


@pytest.mark.asyncio
async def test_no_authority_blocks_the_unattended_auto_resume():
    """The stuck sweep resumes with no human in the loop; it must refuse."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from src.mcp_handlers.lifecycle import stuck as stuck_mod

    server = _gate_server("phi_history")
    meta = MagicMock()
    meta.status = "paused"
    server.agent_metadata = {"agent-1": meta}

    with patch.object(stuck_mod, "mcp_server", server), patch.object(
        stuck_mod, "ensure_hydrated", new=AsyncMock(return_value=False), create=True
    ), patch.object(
        stuck_mod,
        "_trigger_dialectic_for_stuck_agent",
        new=AsyncMock(return_value={"action": "dialectic_triggered"}),
    ) as dialectic:
        results = await stuck_mod._try_recover_agent(
            {"agent_id": "agent-1", "reason": "critical_margin_timeout"},
            note_cooldown_minutes=0.0,
        )

    assert meta.status == "paused", "auto-resume must not fire without authority"
    assert not any(r.get("action") == "auto_resumed" for r in results)
    dialectic.assert_awaited()
