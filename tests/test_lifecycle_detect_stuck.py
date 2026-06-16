"""
Tests for src/mcp_handlers/lifecycle.py - _detect_stuck_agents function.

Tests the pure detection logic by mocking mcp_server state and monitors.
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, PropertyMock
from types import SimpleNamespace

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def _make_agent_meta(
    status="active",
    last_update=None,
    created_at=None,
    total_updates=5,
    tags=None,
    parent_agent_id=None,
    agent_uuid=None,
    spawn_reason=None,
):
    """Create mock agent metadata."""
    now = datetime.now(timezone.utc)
    meta = SimpleNamespace(
        status=status,
        last_update=(last_update or now).isoformat(),
        created_at=(created_at or now).isoformat(),
        total_updates=total_updates,
        tags=tags or [],
        parent_agent_id=parent_agent_id,
        agent_uuid=agent_uuid,
        spawn_reason=spawn_reason,
    )
    return meta


def _make_monitor(
    coherence=0.55,
    risk=0.3,
    void_active=False,
    void_value=0.0,
    S=0.1,
    coherence_history=None,
):
    """Create mock UNITARESMonitor."""
    state = SimpleNamespace(
        coherence=coherence,
        V=void_value,
        void_active=void_active,
        S=S,
        coherence_history=coherence_history if coherence_history is not None else [],
    )
    monitor = MagicMock()
    monitor.state = state
    monitor.get_metrics.return_value = {"mean_risk": risk}
    return monitor


def _margin_info(margin="comfortable", nearest_edge=None, distance=0.5):
    return {
        "margin": margin,
        "nearest_edge": nearest_edge,
        "distance_to_edge": distance,
    }


# Patches needed for every test (lifecycle_stuck has its own mcp_server)
_PATCHES = {
    "mcp_server": "src.mcp_handlers.lifecycle.stuck.mcp_server",
    "gov_config": "src.mcp_handlers.lifecycle.stuck.GovernanceConfig",
}


class TestDetectStuckAgentsEmpty:

    @patch(_PATCHES["mcp_server"])
    def test_no_agents_returns_empty(self, mock_server):
        mock_server.agent_metadata = {}
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert result == []


class TestDetectStuckAgentsFiltering:

    @patch(_PATCHES["mcp_server"])
    def test_archived_agents_skipped(self, mock_server):
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(status="archived"),
        }
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert result == []

    @patch(_PATCHES["mcp_server"])
    def test_deleted_agents_skipped(self, mock_server):
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(status="deleted"),
        }
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert result == []

    @patch(_PATCHES["mcp_server"])
    def test_non_active_agents_skipped(self, mock_server):
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(status="paused"),
        }
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert result == []

    @patch(_PATCHES["mcp_server"])
    def test_autonomous_agents_skipped(self, mock_server):
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_server.agent_metadata = {
            "lumen": _make_agent_meta(last_update=old_time, tags=["autonomous"]),
        }
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert result == []

    @patch(_PATCHES["mcp_server"])
    def test_embodied_agents_skipped(self, mock_server):
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_server.agent_metadata = {
            "creature": _make_agent_meta(last_update=old_time, tags=["embodied"]),
        }
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert result == []

    @patch(_PATCHES["mcp_server"])
    def test_anima_tag_skipped(self, mock_server):
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_server.agent_metadata = {
            "x": _make_agent_meta(last_update=old_time, tags=["anima"]),
        }
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert result == []

    @patch(_PATCHES["mcp_server"])
    def test_low_update_count_skipped(self, mock_server):
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time, total_updates=0),
        }
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(min_updates=1)
        assert result == []

    @patch(_PATCHES["mcp_server"])
    def test_custom_min_updates(self, mock_server):
        old_time = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time, total_updates=3),
        }
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(min_updates=5)
        assert result == []


class TestDetectStuckAgentsTimeout:

    @patch(_PATCHES["mcp_server"])
    def test_no_monitor_no_stuck(self, mock_server):
        """Agent with no monitor state is NOT flagged as stuck (can't determine margin)."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=45)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time),
        }
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(max_age_minutes=30)
        # Without margin info, we can't determine if agent is stuck
        # Inactivity alone does NOT mean stuck
        assert len(result) == 0

    @patch(_PATCHES["mcp_server"])
    def test_recent_agent_not_stuck(self, mock_server):
        """Agent with recent update is not stuck."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=recent),
        }
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(max_age_minutes=30)
        assert result == []

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_comfortable_margin_not_stuck(self, mock_server, mock_config):
        """Agent past max_age with comfortable margin → NOT stuck (inactivity ≠ stuck)."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=45)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time),
        }
        monitor = _make_monitor()
        mock_server.monitors = {"a1": monitor}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info("comfortable")

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(max_age_minutes=30, include_pattern_detection=False)
        # Inactivity alone does NOT mean stuck - comfortable margin means healthy
        assert len(result) == 0


class TestDetectStuckAgentsMarginBased:

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_critical_margin_timeout(self, mock_server, mock_config):
        """Critical margin + timeout → critical_margin_timeout."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time),
        }
        monitor = _make_monitor(risk=0.8, coherence=0.42)
        mock_server.monitors = {"a1": monitor}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info(
            "critical", nearest_edge="risk", distance=0.02
        )

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            critical_margin_timeout_minutes=5,
            include_pattern_detection=False,
        )
        assert len(result) == 1
        assert result[0]["reason"] == "critical_margin_timeout"
        assert result[0]["margin"] == "critical"
        assert result[0]["nearest_edge"] == "risk"

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_critical_margin_below_timeout_not_stuck(self, mock_server, mock_config):
        """Critical margin but within timeout → not stuck."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=3)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=recent),
        }
        monitor = _make_monitor(risk=0.8)
        mock_server.monitors = {"a1": monitor}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info("critical")

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            critical_margin_timeout_minutes=5,
            max_age_minutes=30,
            include_pattern_detection=False,
        )
        assert result == []

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_tight_margin_timeout(self, mock_server, mock_config):
        """Tight margin + inactivity + degraded state → tight_margin_timeout."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=90)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time, total_updates=100),
        }
        # Use degraded metrics (risk > 0.45) so the stuck check fires
        monitor = _make_monitor(risk=0.5, coherence=0.45)
        mock_server.monitors = {"a1": monitor}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info(
            "tight", nearest_edge="coherence", distance=0.08
        )

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            tight_margin_timeout_minutes=15,
            max_age_minutes=30,
            include_pattern_detection=False,
        )
        assert len(result) == 1
        assert result[0]["reason"] == "tight_margin_timeout"
        assert result[0]["margin"] == "tight"

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_tight_margin_below_timeout_not_stuck(self, mock_server, mock_config):
        """Tight margin within timeout → not stuck."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=recent),
        }
        monitor = _make_monitor()
        mock_server.monitors = {"a1": monitor}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info("tight")

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            tight_margin_timeout_minutes=15,
            max_age_minutes=30,
            include_pattern_detection=False,
        )
        assert result == []


class TestDetectStuckAgentsMultiple:

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_multiple_agents_mixed(self, mock_server, mock_config):
        """Multiple agents with different states - only margin-based issues = stuck."""
        old_45 = datetime.now(timezone.utc) - timedelta(minutes=45)
        old_10 = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent_2 = datetime.now(timezone.utc) - timedelta(minutes=2)

        mock_server.agent_metadata = {
            "idle": _make_agent_meta(last_update=old_45),  # Inactive but healthy margin = NOT stuck
            "critical": _make_agent_meta(last_update=old_10),  # Critical margin = stuck
            "healthy": _make_agent_meta(last_update=recent_2),
            "archived": _make_agent_meta(status="archived", last_update=old_45),
        }
        mock_server.monitors = {
            "idle": _make_monitor(),
            "critical": _make_monitor(risk=0.9),
            "healthy": _make_monitor(),
        }
        mock_server.load_monitor_state.return_value = None

        def margin_side_effect(risk_score, coherence, void_active, void_value=0.0, coherence_history=None):
            if risk_score > 0.7:
                return _margin_info("critical", "risk")
            return _margin_info("comfortable")

        mock_config.compute_proprioceptive_margin.side_effect = margin_side_effect

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(include_pattern_detection=False)

        ids = {r["agent_id"] for r in result}
        # "idle" has comfortable margin - inactivity alone is NOT stuck
        assert "idle" not in ids
        # "critical" has critical margin + timeout - IS stuck
        assert "critical" in ids  # critical_margin_timeout
        assert "healthy" not in ids
        assert "archived" not in ids


class TestDetectStuckAgentsEdgeCases:

    @patch(_PATCHES["mcp_server"])
    def test_invalid_timestamp_skipped(self, mock_server):
        """Agent with unparseable last_update is skipped."""
        meta = _make_agent_meta()
        meta.last_update = "not-a-timestamp"
        mock_server.agent_metadata = {"a1": meta}

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert result == []

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_none_last_update_uses_created_at_with_margin(self, mock_server, mock_config):
        """When last_update is None, uses created_at for age calculation."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        meta = _make_agent_meta(created_at=old_time)
        meta.last_update = None
        meta.created_at = old_time.isoformat()
        mock_server.agent_metadata = {"a1": meta}
        mock_server.monitors = {"a1": _make_monitor(risk=0.9)}
        mock_server.load_monitor_state.return_value = None
        mock_config.compute_proprioceptive_margin.return_value = _margin_info("critical", "risk")

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(critical_margin_timeout_minutes=5)
        assert len(result) == 1
        assert result[0]["reason"] == "critical_margin_timeout"

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_z_suffix_timestamp_handled(self, mock_server, mock_config):
        """Timestamps with Z suffix are correctly parsed."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        meta = _make_agent_meta()
        meta.last_update = old_time.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        mock_server.agent_metadata = {"a1": meta}
        mock_server.monitors = {"a1": _make_monitor(risk=0.9)}
        mock_server.load_monitor_state.return_value = None
        mock_config.compute_proprioceptive_margin.return_value = _margin_info("critical", "risk")

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(critical_margin_timeout_minutes=5)
        assert len(result) == 1
        assert result[0]["reason"] == "critical_margin_timeout"

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_monitor_exception_does_not_flag_stuck(self, mock_server, mock_config):
        """If monitor.get_metrics raises, agent is NOT flagged as stuck (can't determine margin)."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=45)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time),
        }
        monitor = MagicMock()
        monitor.get_metrics.side_effect = RuntimeError("broken")
        mock_server.monitors = {"a1": monitor}

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(max_age_minutes=30, include_pattern_detection=False)
        # Without margin info, we can't know if agent is stuck - don't assume
        assert len(result) == 0

    @patch(_PATCHES["mcp_server"])
    def test_none_tags_handled(self, mock_server):
        """Agent with tags=None should not crash.

        Also verifies that without monitor state (and therefore no margin info),
        we don't spuriously flag agents as stuck. Inactivity ≠ stuck.
        """
        old_time = datetime.now(timezone.utc) - timedelta(minutes=45)
        meta = _make_agent_meta(last_update=old_time)
        meta.tags = None
        mock_server.agent_metadata = {"a1": meta}
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        # Function should not crash with tags=None
        result = _detect_stuck_agents(max_age_minutes=30)
        # Without margin info, can't determine if stuck - don't assume
        assert len(result) == 0

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_persisted_state_used_when_no_monitor(self, mock_server, mock_config):
        """When no in-memory monitor, loads persisted state via the cached factory."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time),
        }
        mock_server.monitors = {}

        # Persisted state exists (truthy sentinel); the fix gates on `is not None`
        persisted_state = MagicMock()
        mock_server.load_monitor_state.return_value = persisted_state

        # stuck.py should now call mcp_server.get_or_create_monitor (cached path)
        # instead of constructing a transient UNITARESMonitor.
        monitor_instance = _make_monitor(risk=0.7, coherence=0.42)
        mock_server.get_or_create_monitor.return_value = monitor_instance

        mock_config.compute_proprioceptive_margin.return_value = _margin_info(
            "critical", "coherence"
        )

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            critical_margin_timeout_minutes=5,
            include_pattern_detection=False,
        )
        assert len(result) == 1
        assert result[0]["reason"] == "critical_margin_timeout"
        # Regression: must use the caching factory, not a transient constructor.
        mock_server.get_or_create_monitor.assert_called_once_with("a1")

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_no_persisted_state_skips_without_calling_factory(self, mock_server, mock_config):
        """Regression: when load_monitor_state returns None, we must skip the agent
        WITHOUT calling get_or_create_monitor (which would otherwise synthesize a
        fresh zero-state monitor and cache it permanently)."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=45)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time),
        }
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            max_age_minutes=30,
            include_pattern_detection=False,
        )
        assert result == []
        mock_server.get_or_create_monitor.assert_not_called()


class TestBaselineRelativeMargin:
    """Test baseline-relative coherence tight threshold in compute_proprioceptive_margin."""

    def test_steady_state_agent_comfortable(self):
        """Agent at ODE steady state (~0.49) with stable history → comfortable, not tight."""
        from config.governance_config import GovernanceConfig
        history = [0.49] * 20
        result = GovernanceConfig.compute_proprioceptive_margin(
            risk_score=0.3,
            coherence=0.49,
            void_active=False,
            void_value=0.0,
            coherence_history=history,
        )
        # absolute_margin = 0.49 - 0.40 = 0.09
        # baseline = 0.49, tight_threshold = max(0.049, 0.03) = 0.049
        # 0.09 > 0.049 → comfortable
        assert result["margin"] == "comfortable"
        assert result["details"]["coherence_tight_threshold"] == pytest.approx(0.049, abs=0.001)

    def test_dropping_agent_tight(self):
        """Agent dropped from 0.80 baseline to 0.44 → tight (absolute margin < adaptive threshold)."""
        from config.governance_config import GovernanceConfig
        history = [0.80] * 20
        result = GovernanceConfig.compute_proprioceptive_margin(
            risk_score=0.3,
            coherence=0.44,
            void_active=False,
            void_value=0.0,
            coherence_history=history,
        )
        # absolute_margin = 0.44 - 0.40 = 0.04
        # baseline = 0.80, tight_threshold = max(0.08, 0.03) = 0.08
        # 0.04 < 0.08 → tight
        assert result["margin"] == "tight"
        assert result["nearest_edge"] == "coherence"
        assert result["details"]["coherence_tight_threshold"] == pytest.approx(0.08, abs=0.001)

    def test_no_history_returns_settling(self):
        """Without coherence_history, returns 'settling' (warmup grace period)."""
        from config.governance_config import GovernanceConfig
        result = GovernanceConfig.compute_proprioceptive_margin(
            risk_score=0.3,
            coherence=0.49,
            void_active=False,
            void_value=0.0,
            coherence_history=None,
        )
        # No history → warmup grace period
        assert result["margin"] == "settling"
        assert result["nearest_edge"] is None

    def test_short_history_falls_back_to_fixed(self):
        """With < 10 history entries, uses fixed 0.15 threshold."""
        from config.governance_config import GovernanceConfig
        history = [0.49] * 5
        result = GovernanceConfig.compute_proprioceptive_margin(
            risk_score=0.3,
            coherence=0.49,
            void_active=False,
            void_value=0.0,
            coherence_history=history,
        )
        assert result["margin"] == "tight"
        assert result["details"]["coherence_tight_threshold"] == 0.15

    def test_threshold_floor_at_003(self):
        """Adaptive threshold has a floor of 0.03 even with very low baseline."""
        from config.governance_config import GovernanceConfig
        history = [0.20] * 20  # very low baseline
        result = GovernanceConfig.compute_proprioceptive_margin(
            risk_score=0.3,
            coherence=0.42,
            void_active=False,
            void_value=0.0,
            coherence_history=history,
        )
        # baseline = 0.20, 10% = 0.02, floor = 0.03
        assert result["details"]["coherence_tight_threshold"] == 0.03

    def test_risk_edge_uses_fixed_threshold(self):
        """When risk is the nearest edge, fixed 0.15 is used regardless of coherence history."""
        from config.governance_config import GovernanceConfig
        history = [0.80] * 20
        result = GovernanceConfig.compute_proprioceptive_margin(
            risk_score=0.60,  # risk_margin = 0.70 - 0.60 = 0.10 (nearest edge)
            coherence=0.80,   # coherence_margin = 0.40 (far away)
            void_active=False,
            void_value=0.0,
            coherence_history=history,
        )
        # risk is nearest edge at 0.10, uses fixed 0.15 → 0.10 < 0.15 → tight
        assert result["margin"] == "tight"
        assert result["nearest_edge"] == "risk"

    def test_crossed_threshold_unaffected(self):
        """Crossed thresholds (negative margins) still produce warning/critical as before."""
        from config.governance_config import GovernanceConfig
        history = [0.80] * 20
        result = GovernanceConfig.compute_proprioceptive_margin(
            risk_score=0.3,
            coherence=0.38,  # below 0.40 critical threshold
            void_active=False,
            void_value=0.0,
            coherence_history=history,
        )
        assert result["margin"] == "warning"
        assert result["nearest_edge"] == "coherence"
        assert result["distance_to_edge"] < 0


class TestDetectStuckAgentsCadenceSilence:
    """Rule 5 (soft): an agent that HAD an active cadence then went silent."""

    def _silent_meta(self, now, *, created_min_ago, last_update_min_ago, total_updates):
        return _make_agent_meta(
            created_at=now - timedelta(minutes=created_min_ago),
            last_update=now - timedelta(minutes=last_update_min_ago),
            total_updates=total_updates,
        )

    @patch(_PATCHES["mcp_server"])
    def test_active_cadence_then_silent_flags_soft(self, mock_server):
        # 13 updates over ~10 min (avg gap ~0.8 min => active), then silent 50 min.
        now = datetime.now(timezone.utc)
        mock_server.agent_metadata = {
            "a1": self._silent_meta(now, created_min_ago=60, last_update_min_ago=50, total_updates=13),
        }
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        cadence = [r for r in result if r["reason"] == "cadence_silence"]
        assert len(cadence) == 1
        assert cadence[0]["soft"] is True
        assert cadence[0]["agent_id"] == "a1"

    @patch(_PATCHES["mcp_server"])
    def test_orphan_below_min_updates_not_flagged(self, mock_server):
        # Only 3 updates => below CADENCE_MIN_UPDATES (5) => no active cadence.
        now = datetime.now(timezone.utc)
        mock_server.agent_metadata = {
            "a1": self._silent_meta(now, created_min_ago=60, last_update_min_ago=50, total_updates=3),
        }
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert [r for r in result if r["reason"] == "cadence_silence"] == []

    @patch(_PATCHES["mcp_server"])
    def test_slow_cron_cadence_not_flagged(self, mock_server):
        # 6 updates over ~24h => avg gap ~270 min >> 30 => not an active cadence.
        now = datetime.now(timezone.utc)
        mock_server.agent_metadata = {
            "a1": self._silent_meta(now, created_min_ago=24 * 60, last_update_min_ago=90, total_updates=6),
        }
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert [r for r in result if r["reason"] == "cadence_silence"] == []

    @patch(_PATCHES["mcp_server"])
    def test_active_cadence_recent_silence_not_flagged(self, mock_server):
        # Active cadence but only silent 10 min (< 30 min floor) => not yet flagged.
        now = datetime.now(timezone.utc)
        mock_server.agent_metadata = {
            "a1": self._silent_meta(now, created_min_ago=60, last_update_min_ago=10, total_updates=13),
        }
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert [r for r in result if r["reason"] == "cadence_silence"] == []

    @patch(_PATCHES["mcp_server"])
    def test_stale_silence_beyond_cap_not_flagged(self, mock_server):
        # Active cadence but silent ~25h (> 24h stale cap) => abandoned, not a
        # fresh hang => suppressed (the false-positive-noise guard).
        now = datetime.now(timezone.utc)
        mock_server.agent_metadata = {
            "a1": self._silent_meta(now, created_min_ago=26 * 60, last_update_min_ago=25 * 60, total_updates=13),
        }
        mock_server.monitors = {}
        mock_server.load_monitor_state.return_value = None
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents()
        assert [r for r in result if r["reason"] == "cadence_silence"] == []

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_silent_agent_not_double_listed_with_margin(self, mock_server, mock_config):
        # Fires cadence_silence AND has a (stale) critical-margin monitor: must
        # appear ONCE (cadence_silence), not also as a margin entry — the
        # `continue` after the cadence append is what guarantees this.
        now = datetime.now(timezone.utc)
        mock_server.agent_metadata = {
            "a1": self._silent_meta(now, created_min_ago=60, last_update_min_ago=50, total_updates=13),
        }
        mock_server.monitors = {"a1": _make_monitor(risk=0.8, coherence=0.42)}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info(
            "critical", nearest_edge="risk", distance=0.02
        )
        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = [r for r in _detect_stuck_agents(critical_margin_timeout_minutes=5) if r["agent_id"] == "a1"]
        assert len(result) == 1
        assert result[0]["reason"] == "cadence_silence"


class TestMarginStaleCap:
    """Margin timeout rules must not fire on abandoned identities (> 24h idle).

    An abandoned agent's margin is computed from its frozen state — no new
    check-ins ever arrive to move it off critical — so without the cap it is
    "stuck" forever (observed 2026-06-12: ~24 day-old identities pinned the
    stuck KPI and re-fired the audit trail on every sweep).
    """

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_abandoned_critical_margin_suppressed(self, mock_server, mock_config):
        """Critical margin but idle > MARGIN_STUCK_STALE_CAP_MINUTES → not stuck."""
        old_time = datetime.now(timezone.utc) - timedelta(hours=25)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time),
        }
        mock_server.monitors = {"a1": _make_monitor(risk=0.8, coherence=0.42)}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info(
            "critical", nearest_edge="risk", distance=0.02
        )

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            critical_margin_timeout_minutes=5,
            include_pattern_detection=False,
        )
        assert result == []

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_old_but_under_cap_still_stuck(self, mock_server, mock_config):
        """Idle 23h (< cap) with critical margin → still detected."""
        old_time = datetime.now(timezone.utc) - timedelta(hours=23)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(last_update=old_time),
        }
        mock_server.monitors = {"a1": _make_monitor(risk=0.8, coherence=0.42)}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info(
            "critical", nearest_edge="risk", distance=0.02
        )

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            critical_margin_timeout_minutes=5,
            include_pattern_detection=False,
        )
        assert len(result) == 1
        assert result[0]["reason"] == "critical_margin_timeout"


class TestStuckAuditDedupe:
    """stuck_detected audit writes are emit-on-change (stuck_change_token).

    The handler runs on every dashboard refresh plus a 5-min background sweep;
    unconditional writes produced ~250 identical audit rows/hour (2026-06-12).
    """

    def setup_method(self):
        import src.mcp_handlers.lifecycle.stuck as stuck_module
        stuck_module._last_stuck_audit_token = None

    def _stuck_set(self, *ids_reasons):
        return [
            {"agent_id": aid, "reason": reason, "age_minutes": 10.0}
            for aid, reason in ids_reasons
        ]

    def test_token_stable_across_order(self):
        from src.mcp_handlers.lifecycle.stuck import stuck_change_token
        a = self._stuck_set(("a1", "critical_margin_timeout"), ("a2", "cadence_silence"))
        b = self._stuck_set(("a2", "cadence_silence"), ("a1", "critical_margin_timeout"))
        assert stuck_change_token(a) == stuck_change_token(b)

    def test_token_changes_on_membership_and_reason(self):
        from src.mcp_handlers.lifecycle.stuck import stuck_change_token
        base = self._stuck_set(("a1", "critical_margin_timeout"))
        grown = self._stuck_set(("a1", "critical_margin_timeout"), ("a2", "cadence_silence"))
        reasoned = self._stuck_set(("a1", "tight_margin_timeout"))
        assert stuck_change_token(base) != stuck_change_token(grown)
        assert stuck_change_token(base) != stuck_change_token(reasoned)

    @pytest.mark.asyncio
    async def test_unchanged_set_writes_audit_once(self):
        stuck_set = self._stuck_set(("a1", "critical_margin_timeout"))
        with patch(_PATCHES["mcp_server"]) as mock_server, \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", return_value=stuck_set) as mock_detect, \
             patch("src.audit_log.audit_logger._write_entry") as mock_write:
            mock_server.load_metadata_async = MagicMock(return_value=_async_none())
            from src.mcp_handlers.lifecycle.stuck import handle_detect_stuck_agents
            await handle_detect_stuck_agents({})
            # Pin the executor argument order: (max_age, critical_timeout,
            # tight_timeout, include_patterns, min_updates)
            mock_detect.assert_called_once_with(30.0, 5.0, 15.0, True, 1)
            mock_server.load_metadata_async = MagicMock(return_value=_async_none())
            await handle_detect_stuck_agents({})
            assert mock_write.call_count == 1

    @pytest.mark.asyncio
    async def test_changed_set_writes_again(self):
        first = self._stuck_set(("a1", "critical_margin_timeout"))
        second = self._stuck_set(("a1", "critical_margin_timeout"), ("a2", "cadence_silence"))
        with patch(_PATCHES["mcp_server"]) as mock_server, \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", side_effect=[first, second]), \
             patch("src.audit_log.audit_logger._write_entry") as mock_write:
            mock_server.load_metadata_async = MagicMock(side_effect=lambda: _async_none())
            from src.mcp_handlers.lifecycle.stuck import handle_detect_stuck_agents
            await handle_detect_stuck_agents({})
            await handle_detect_stuck_agents({})
            assert mock_write.call_count == 2

    @pytest.mark.asyncio
    async def test_cleared_set_resets_token(self):
        """non-empty → empty → same non-empty set again must log twice."""
        stuck_set = self._stuck_set(("a1", "critical_margin_timeout"))
        with patch(_PATCHES["mcp_server"]) as mock_server, \
             patch("src.mcp_handlers.lifecycle.stuck._detect_stuck_agents", side_effect=[stuck_set, [], stuck_set]), \
             patch("src.audit_log.audit_logger._write_entry") as mock_write:
            mock_server.load_metadata_async = MagicMock(side_effect=lambda: _async_none())
            from src.mcp_handlers.lifecycle.stuck import handle_detect_stuck_agents
            await handle_detect_stuck_agents({})
            await handle_detect_stuck_agents({})
            await handle_detect_stuck_agents({})
            assert mock_write.call_count == 2


def _async_none():
    async def _coro():
        return None
    return _coro()


class TestLineageSuccession:
    """A parent whose declared-lineage child is actively checking in is not
    stuck — the process rotated, lineage continuous (KG 2026-05-06)."""

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_parent_with_live_child_suppressed(self, mock_server, mock_config):
        """Parent would fire critical_margin_timeout, but a live lineage child
        checking in suppresses it entirely."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        mock_server.agent_metadata = {
            "p1": _make_agent_meta(last_update=old_time),
            "c1": _make_agent_meta(last_update=recent, parent_agent_id="p1"),
        }
        monitor = _make_monitor(risk=0.8, coherence=0.42)
        mock_server.monitors = {"p1": monitor, "c1": _make_monitor()}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info(
            "critical", nearest_edge="risk", distance=0.02
        )

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            critical_margin_timeout_minutes=5, include_pattern_detection=False
        )
        # p1 suppressed by lineage; c1 is recent + healthy → neither stuck.
        assert result == []

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_parent_matched_by_agent_uuid(self, mock_server, mock_config):
        """Child declares the parent's agent_uuid (not the dict key) → suppressed."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        mock_server.agent_metadata = {
            "p1": _make_agent_meta(last_update=old_time, agent_uuid="uuid-parent"),
            "c1": _make_agent_meta(last_update=recent, parent_agent_id="uuid-parent"),
        }
        mock_server.monitors = {"p1": _make_monitor(risk=0.8), "c1": _make_monitor()}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info("critical")

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            critical_margin_timeout_minutes=5, include_pattern_detection=False
        )
        assert result == []

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_stale_child_does_not_suppress(self, mock_server, mock_config):
        """A child that itself went silent (beyond the freshness window) does NOT
        explain the parent's silence → parent still flagged."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        stale_child = datetime.now(timezone.utc) - timedelta(minutes=90)
        mock_server.agent_metadata = {
            "p1": _make_agent_meta(last_update=old_time),
            "c1": _make_agent_meta(
                last_update=stale_child, parent_agent_id="p1", total_updates=2
            ),
        }
        mock_server.monitors = {"p1": _make_monitor(risk=0.8, coherence=0.42)}
        mock_server.load_monitor_state.return_value = None
        mock_config.compute_proprioceptive_margin.return_value = _margin_info(
            "critical", nearest_edge="risk", distance=0.02
        )

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            critical_margin_timeout_minutes=5, include_pattern_detection=False
        )
        assert len(result) == 1
        assert result[0]["agent_id"] == "p1"
        assert result[0]["reason"] == "critical_margin_timeout"

    @patch(_PATCHES["gov_config"])
    @patch(_PATCHES["mcp_server"])
    def test_inactive_child_does_not_suppress(self, mock_server, mock_config):
        """A non-active (paused/archived) child is not a live successor."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        mock_server.agent_metadata = {
            "p1": _make_agent_meta(last_update=old_time),
            "c1": _make_agent_meta(
                status="paused", last_update=recent, parent_agent_id="p1"
            ),
        }
        mock_server.monitors = {"p1": _make_monitor(risk=0.8, coherence=0.42)}
        mock_config.compute_proprioceptive_margin.return_value = _margin_info(
            "critical", nearest_edge="risk", distance=0.02
        )

        from src.mcp_handlers.lifecycle.stuck import _detect_stuck_agents
        result = _detect_stuck_agents(
            critical_margin_timeout_minutes=5, include_pattern_detection=False
        )
        assert len(result) == 1
        assert result[0]["agent_id"] == "p1"

    @patch(_PATCHES["mcp_server"])
    def test_self_referential_parent_id_ignored(self, mock_server):
        """A child whose parent_agent_id points at its own id is not a successor
        of itself — must not be added to the live-parent set."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        mock_server.agent_metadata = {
            "a1": _make_agent_meta(
                last_update=recent, parent_agent_id="a1", agent_uuid="a1"
            ),
        }
        from src.mcp_handlers.lifecycle.stuck import _live_lineage_parent_ids
        live = _live_lineage_parent_ids(datetime.now(timezone.utc))
        assert live == set()

    @patch(_PATCHES["mcp_server"])
    def test_subagent_child_does_not_supersede_parent(self, mock_server):
        """A dispatched subagent has a LIVE parent by definition (the
        dispatcher mid-dispatch) — its presence must not mark that parent
        superseded. Regression for 2026-06-16: a council of subagents archived
        the live main session, which had ~12.9k updates and had checked in
        seconds before, because each subagent declared it as parent."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        mock_server.agent_metadata = {
            "sub1": _make_agent_meta(
                last_update=recent, parent_agent_id="main", spawn_reason="subagent"
            ),
        }
        from src.mcp_handlers.lifecycle.stuck import _live_lineage_parent_ids
        live = _live_lineage_parent_ids(datetime.now(timezone.utc))
        assert live == set()

    @patch(_PATCHES["mcp_server"])
    def test_compaction_child_does_not_supersede_parent(self, mock_server):
        """A compaction fork also has a live parent by design — same exemption."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        mock_server.agent_metadata = {
            "fork1": _make_agent_meta(
                last_update=recent, parent_agent_id="main", spawn_reason="compaction"
            ),
        }
        from src.mcp_handlers.lifecycle.stuck import _live_lineage_parent_ids
        live = _live_lineage_parent_ids(datetime.now(timezone.utc))
        assert live == set()

    @patch(_PATCHES["mcp_server"])
    def test_explicit_handoff_child_still_supersedes_parent(self, mock_server):
        """A genuine serial handoff (spawn_reason='explicit', from an EXITED
        predecessor) is still a real succession — it must NOT be exempted, or
        we'd never retire legitimately-handed-off predecessors."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        mock_server.agent_metadata = {
            "succ": _make_agent_meta(
                last_update=recent, parent_agent_id="pred", spawn_reason="explicit"
            ),
        }
        from src.mcp_handlers.lifecycle.stuck import _live_lineage_parent_ids
        live = _live_lineage_parent_ids(datetime.now(timezone.utc))
        assert live == {"pred"}

    @pytest.mark.asyncio
    async def test_archive_superseded_parents_archives_parent(self):
        """auto_recover sweep retires a superseded parent as lineage_succession."""
        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        with patch(_PATCHES["mcp_server"]) as mock_server, \
             patch("src.mcp_handlers.lifecycle.helpers._archive_one_agent") as mock_arch, \
             patch("src.mcp_handlers.identity.process_binding.get_live_bindings") as mock_live:
            mock_server.agent_metadata = {
                "p1": _make_agent_meta(last_update=old_time),
                "c1": _make_agent_meta(last_update=recent, parent_agent_id="p1"),
            }
            mock_server.monitors = {}

            async def _ok(*a, **k):
                return True
            mock_arch.side_effect = _ok

            async def _no_bindings(*a, **k):
                return []
            mock_live.side_effect = _no_bindings

            from src.mcp_handlers.lifecycle.stuck import _archive_superseded_parents
            results = await _archive_superseded_parents(datetime.now(timezone.utc))

        assert len(results) == 1
        assert results[0]["agent_id"] == "p1"
        assert results[0]["reason"] == "lineage_succession"
        # The child must never be archived — it is the live successor.
        archived_ids = [c.args[0] for c in mock_arch.call_args_list]
        assert archived_ids == ["p1"]

    @pytest.mark.asyncio
    async def test_archive_superseded_parents_skips_initializing_ghost(self):
        """A parent that has never checked in (total_updates == 0) is an
        initializing ghost and must NOT be archived even when a live child
        declares it — declaring parent_agent_id attests ancestry, not exit.

        Regression for the 2026-06-14 incident: a fresh session working but
        not yet checked in was archived out from under itself the moment a
        concurrent same-workspace sibling onboarded declaring it parent.
        Mirrors classify_for_archival's ghost protection.
        """
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        with patch(_PATCHES["mcp_server"]) as mock_server, \
             patch("src.mcp_handlers.lifecycle.helpers._archive_one_agent") as mock_arch, \
             patch("src.mcp_handlers.identity.process_binding.get_live_bindings") as mock_live:
            mock_server.agent_metadata = {
                # ghost: superseded by a live child but 0 check-ins → protected
                "ghost": _make_agent_meta(last_update=recent, total_updates=0),
                "c_ghost": _make_agent_meta(
                    last_update=recent, parent_agent_id="ghost"
                ),
                # checked-in parent superseded by a live child → still archived
                "done": _make_agent_meta(last_update=recent, total_updates=4),
                "c_done": _make_agent_meta(
                    last_update=recent, parent_agent_id="done"
                ),
            }
            mock_server.monitors = {}

            async def _ok(*a, **k):
                return True
            mock_arch.side_effect = _ok

            async def _no_bindings(*a, **k):
                return []
            mock_live.side_effect = _no_bindings

            from src.mcp_handlers.lifecycle.stuck import _archive_superseded_parents
            results = await _archive_superseded_parents(datetime.now(timezone.utc))

        archived_ids = [c.args[0] for c in mock_arch.call_args_list]
        # ghost protected; only the checked-in superseded parent retired.
        assert archived_ids == ["done"]
        assert [r["agent_id"] for r in results] == ["done"]

    @pytest.mark.asyncio
    async def test_archive_superseded_parents_skips_parent_of_subagents(self):
        """End-to-end regression for the 2026-06-16 incident: a live parent
        whose only 'superseding' children are dispatched subagents must NOT be
        archived — even with no live process binding (the parent's fingerprint
        was churning, so the binding signal alone didn't protect it). The
        spawn_reason exemption keeps the parent out of the superseded set
        entirely, before the binding gate is ever consulted."""
        recent = datetime.now(timezone.utc) - timedelta(minutes=1)
        with patch(_PATCHES["mcp_server"]) as mock_server, \
             patch("src.mcp_handlers.lifecycle.helpers._archive_one_agent") as mock_arch, \
             patch("src.mcp_handlers.identity.process_binding.get_live_bindings") as mock_live:
            mock_server.agent_metadata = {
                # the main session — actively working, many updates
                "main": _make_agent_meta(last_update=recent, total_updates=12900),
                "sub1": _make_agent_meta(
                    last_update=recent, parent_agent_id="main", spawn_reason="subagent"
                ),
                "sub2": _make_agent_meta(
                    last_update=recent, parent_agent_id="main", spawn_reason="subagent"
                ),
            }
            mock_server.monitors = {}

            async def _ok(*a, **k):
                return True
            mock_arch.side_effect = _ok

            async def _no_bindings(*a, **k):
                return []  # fingerprint churn → no live binding seen
            mock_live.side_effect = _no_bindings

            from src.mcp_handlers.lifecycle.stuck import _archive_superseded_parents
            results = await _archive_superseded_parents(datetime.now(timezone.utc))

        # The parent of subagents is never superseded → nothing archived.
        assert mock_arch.call_args_list == []
        assert results == []

    @pytest.mark.asyncio
    async def test_archive_superseded_parents_skips_live_process_binding(self):
        """A checked-in parent that is STILL a running process (live process
        binding) must NOT be archived even when a live child declares it —
        lineage declaration attests ancestry, not that the parent exited.

        Covers the residual hole the updates==0 guard alone leaves: a parent
        that checked in (total_updates >= 1) then works silently past the
        succession window (long tool call / subagent dispatch) while a sibling
        onboards declaring it parent. The live process binding is the
        conceptually-correct liveness signal (architect council finding).
        """
        recent = datetime.now(timezone.utc) - timedelta(minutes=2)
        with patch(_PATCHES["mcp_server"]) as mock_server, \
             patch("src.mcp_handlers.lifecycle.helpers._archive_one_agent") as mock_arch, \
             patch("src.mcp_handlers.identity.process_binding.get_live_bindings") as mock_live:
            mock_server.agent_metadata = {
                # checked-in parent, superseded by a live child, but its own
                # process is still alive → must survive.
                "alive": _make_agent_meta(last_update=recent, total_updates=7),
                "c_alive": _make_agent_meta(
                    last_update=recent, parent_agent_id="alive"
                ),
            }
            mock_server.monitors = {}

            async def _ok(*a, **k):
                return True
            mock_arch.side_effect = _ok

            async def _live_for_alive(agent_id, *a, **k):
                return [{"pid": 123, "last_seen": recent.isoformat()}] if agent_id == "alive" else []
            mock_live.side_effect = _live_for_alive

            from src.mcp_handlers.lifecycle.stuck import _archive_superseded_parents
            results = await _archive_superseded_parents(datetime.now(timezone.utc))

        # The parent's live binding protects it — nothing archived.
        assert mock_arch.call_args_list == []
        assert results == []
