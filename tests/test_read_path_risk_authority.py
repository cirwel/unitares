"""The read path must report the risk the verdict was made from.

`monitor_risk.estimate_risk` appends the Φ-DERIVED risk to
`state.risk_history`. Under `UNITARES_PHI_TELEMETRY_ONLY` (default on) Φ is
demoted to telemetry: `resolve_verdict_risk` makes the behavioral assessment
authoritative, and that resolved value is what reaches `metrics['risk_score']`
and the persisted `core.agent_state` row. Nothing appended it to the history.

So `get_monitor_metrics` — which serves `get_governance_metrics`, i.e. every
dashboard poll — averaged the demoted signal and labelled it `risk_score`,
then fed it to `HealthThresholds`. Measured live 2026-08-13: Lumen read
0.744 / "critical" on that path while the row written by the same check-in
said 0.09 / "healthy". Fleet-wide, not agent-specific.

Same shape as the coherence gap — a migration promoted the behavioral signal
and left one consumer reading the demoted one.
"""

import sys
from pathlib import Path

import pytest

project_root_path = Path(__file__).parent.parent
sys.path.insert(0, str(project_root_path))

from src.governance_monitor import UNITARESMonitor
from src.monitor_metrics import get_monitor_metrics


def _assessed_monitor(agent_id):
    monitor = UNITARESMonitor(agent_id=agent_id, load_state=False)
    monitor.process_update(
        {
            "response_text": "ordinary work",
            "complexity": 0.3,
            "parameters": [0.5] * 128,
        }
    )
    return monitor


class TestReadPathRiskAuthority:

    def test_read_path_matches_the_checkin_risk(self):
        """metrics['risk_score'] from the read path == the check-in's risk."""
        monitor = _assessed_monitor("risk_authority_match")
        checkin_risk = monitor._last_resolved_risk
        assert checkin_risk is not None

        read = get_monitor_metrics(monitor, include_state=False)
        assert read["risk_score"] == pytest.approx(checkin_risk)
        assert read["latest_risk_score"] == pytest.approx(checkin_risk)
        assert read["risk_score_source"] == "resolved"

    def test_read_path_does_not_average_phi_history(self):
        """A Φ history that disagrees with the verdict must not drive the read.

        This is the live failure reproduced: history pinned high, resolved risk
        low. Before the fix the read reported the history.
        """
        monitor = _assessed_monitor("risk_authority_phi_ignored")
        monitor.state.risk_history = [0.74] * 20
        monitor._last_resolved_risk = 0.09

        read = get_monitor_metrics(monitor, include_state=False)
        assert read["risk_score"] == pytest.approx(0.09)
        assert read["risk_score_source"] == "resolved"
        # The Φ trend stays available, just not as the headline.
        assert read["mean_risk"] == pytest.approx(0.74)
        assert read["current_risk"] == pytest.approx(0.74)

    def test_health_status_follows_the_resolved_risk(self):
        """The 🔴 in the dashboard came from the demoted signal, not the verdict."""
        monitor = _assessed_monitor("risk_authority_status")
        monitor.state.risk_history = [0.95] * 20
        monitor._last_resolved_risk = 0.02
        monitor.state.void_active = False

        read = get_monitor_metrics(monitor, include_state=False)
        assert read["status"] != "critical"

    def test_falls_back_to_phi_before_any_assessment(self):
        """A monitor that has not assessed yet has no resolved risk to report.

        Cold start / restored snapshot: the Φ prior is the honest answer, and it
        must be labelled as such rather than silently passed off as the verdict.
        """
        monitor = UNITARESMonitor(agent_id="risk_authority_coldstart", load_state=False)
        assert getattr(monitor, "_last_resolved_risk", None) is None
        monitor.state.risk_history = [0.5, 0.5, 0.5]
        monitor.state.update_count = 1  # not uninitialized, but never assessed

        read = get_monitor_metrics(monitor, include_state=False)
        assert read["risk_score"] == pytest.approx(0.5)
        assert read["risk_score_source"] == "phi_history"

    def test_uninitialized_reports_no_source(self):
        monitor = UNITARESMonitor(agent_id="risk_authority_uninit", load_state=False)
        read = get_monitor_metrics(monitor, include_state=False)
        assert read["risk_score"] is None
        assert read["risk_score_source"] is None
