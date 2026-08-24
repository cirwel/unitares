"""Three-arm synthetic restart ablation for the risk-authority boundary."""

from __future__ import annotations

import json

import pytest

import src._imports as project_imports
import src.agent_monitor_state as monitor_state
from src.governance_monitor import UNITARESMonitor
from src.mcp_handlers.lifecycle.recovery_policy import authoritative_risk_score


@pytest.mark.parametrize(
    (
        "agent_id",
        "resolved_risk",
        "resolved_verdict",
        "phi_risk",
        "expected_headline_risk",
        "expected_source",
        "expected_recovery_risk",
        "expected_automatic_recovery_risk",
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
            0.50,
            1.00,
            id="phi-only-fails-closed-for-recovery",
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
    expected_recovery_risk,
    expected_automatic_recovery_risk,
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
    assert authoritative_risk_score(metrics, default=0.50) == pytest.approx(
        expected_recovery_risk
    )
    assert authoritative_risk_score(metrics, default=1.00) == pytest.approx(
        expected_automatic_recovery_risk
    )

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
