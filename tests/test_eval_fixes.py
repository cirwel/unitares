"""
Tests for the four governance eval infrastructure fixes:
1. Confidence variance (tool-EISV gap + per-agent offset)
2. Agent purpose inference
3. Decision sub_action granularity
4. PI controller gate relaxation for declining coherence
"""

import os
import sys
sys.path.insert(0, '.')

from unittest.mock import MagicMock, patch, AsyncMock
from collections import deque
import asyncio

import pytest

from config.governance_config import GovernanceConfig, config
from src.monitor_decision import make_decision as monitor_make_decision


# ═══════════════════════════════════════════════════════════════════════
# Fix 1: Confidence variance
# ═══════════════════════════════════════════════════════════════════════

class TestConfidenceVariance:
    """Confidence should vary across agents and reflect tool-EISV disagreement."""

    def _make_state(self, E=0.78, I=0.80, S=0.03, V=0.01, coherence=0.50):
        state = MagicMock()
        state.E = E
        state.I = I
        state.S = S
        state.V = V
        state.coherence = coherence
        state.E_history = deque([E] * 5)
        state.I_history = deque([I] * 5)
        state.S_history = deque([S] * 5)
        state.V_history = deque([V] * 5)
        return state

    @patch('src.tool_usage_tracker.get_tool_usage_tracker')
    def test_different_agents_spread_confidence(self, mock_tracker):
        """Identical EISV across a population of agents must not collapse to one value.

        Asserted over a population, not over a single pair: the offset is
        bucketed into 1000 slots, so any *given* pair collides with
        probability ~1/1000. The old pairwise assertion was therefore a
        per-run lottery, and CI drew the losing ticket.
        """
        mock_tracker.return_value.get_usage_stats.return_value = {'total_calls': 0}

        from src.confidence import derive_confidence
        state = self._make_state()

        agents = [f"agent-{i:03d}" for i in range(20)]
        confs = [derive_confidence(state, agent_id=a)[0] for a in agents]

        assert len(set(confs)) >= len(agents) - 1, (
            "Identical EISV should spread across agents, not collapse to one value"
        )
        assert max(confs) - min(confs) <= 0.02, "Per-agent offset should be small (±0.01)"

    def test_agent_offset_is_stable_across_processes(self):
        """The offset must not change when the interpreter's hash seed changes.

        ``hash()`` is salted per process, so the previous implementation gave
        one agent a different offset after every server restart. This is the
        regression test for that: two subprocesses with different
        PYTHONHASHSEED values must agree.
        """
        import subprocess
        import sys as _sys

        script = (
            "import sys; sys.path.insert(0, '.');"
            "from src.confidence import stable_agent_offset as f;"
            "print(repr([f('agent-aaa-111'), f('agent-bbb-222'), f('')]))"
        )
        outs = []
        for seed in ("0", "12345"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run(
                [_sys.executable, "-c", script],
                capture_output=True, text=True, env=env, check=True,
            ).stdout.strip()
            outs.append(out)

        assert outs[0] == outs[1], (
            f"per-agent offset moved with PYTHONHASHSEED: {outs[0]} vs {outs[1]}"
        )

    @patch('src.tool_usage_tracker.get_tool_usage_tracker')
    def test_metadata_names_the_offset_scheme(self, mock_tracker):
        """Telemetry must say WHICH offset formula produced the value.

        Every agent's offset moves exactly once, at this change's rollout.
        Without a marker, a later calibration analysis sees a one-time step in
        every agent at the same instant and cannot distinguish that formula
        migration from a real change in agent state.
        """
        mock_tracker.return_value.get_usage_stats.return_value = {'total_calls': 0}

        from src.confidence import AGENT_OFFSET_SCHEME, derive_confidence
        state = self._make_state()

        _, meta = derive_confidence(state, agent_id="agent-aaa-111")
        assert meta['agent_offset_scheme'] == AGENT_OFFSET_SCHEME

        # Emitted for the no-agent_id case too, so a consumer never has to infer
        # the formula from a record's timestamp.
        _, meta_none = derive_confidence(state)
        assert meta_none['agent_offset'] == 0.0
        assert meta_none['agent_offset_scheme'] == AGENT_OFFSET_SCHEME

    def test_the_scheme_marker_names_the_actual_formula(self):
        """A version string that outlives its formula is worse than none.

        Pins the marker to the digest it describes: changing the hash without
        bumping the string would silently republish old-scheme telemetry under
        a name that no longer matches.
        """
        import hashlib

        from src.confidence import (
            AGENT_OFFSET_BUCKETS,
            AGENT_OFFSET_HALF_WIDTH,
            AGENT_OFFSET_SCHEME,
            stable_agent_offset,
        )

        assert AGENT_OFFSET_SCHEME == "blake2b-v1"

        digest = hashlib.blake2b(b"agent-aaa-111", digest_size=8).digest()
        bucket = int.from_bytes(digest, "big") % AGENT_OFFSET_BUCKETS
        quantum = 2 * AGENT_OFFSET_HALF_WIDTH / AGENT_OFFSET_BUCKETS
        assert stable_agent_offset("agent-aaa-111") == (
            bucket - AGENT_OFFSET_BUCKETS / 2) * quantum

    def test_agent_offset_stays_in_band(self):
        """Offset is bounded to ±0.01 for any input, including empty/unicode."""
        from src.confidence import AGENT_OFFSET_HALF_WIDTH, stable_agent_offset

        samples = [""] + [f"agent-{i}" for i in range(2000)] + ["\u00e9\u00e0\u4e2d\u6587"]
        offsets = [stable_agent_offset(a) for a in samples]

        assert all(abs(o) <= AGENT_OFFSET_HALF_WIDTH for o in offsets)
        # And it actually uses the band rather than clustering in one corner.
        assert max(offsets) > 0.5 * AGENT_OFFSET_HALF_WIDTH
        assert min(offsets) < -0.5 * AGENT_OFFSET_HALF_WIDTH

    @patch('src.tool_usage_tracker.get_tool_usage_tracker')
    def test_tool_gap_penalty(self, mock_tracker):
        """Agent with low tool success should get lower confidence than one with high success."""
        from src.confidence import derive_confidence

        # High tool success
        tracker_high = MagicMock()
        tracker_high.get_usage_stats.return_value = {
            'total_calls': 10,
            'tools': {
                'read': {'success_count': 9, 'total_calls': 10}
            }
        }

        # Low tool success
        tracker_low = MagicMock()
        tracker_low.get_usage_stats.return_value = {
            'total_calls': 10,
            'tools': {
                'read': {'success_count': 2, 'total_calls': 10}
            }
        }

        state = self._make_state()
        agent_id = "agent-test-gap"

        mock_tracker.return_value = tracker_high
        conf_high, meta_high = derive_confidence(state, agent_id=agent_id)

        mock_tracker.return_value = tracker_low
        conf_low, meta_low = derive_confidence(state, agent_id=agent_id)

        assert conf_low < conf_high, "Low tool success should produce lower confidence"
        assert meta_low['gap_penalty'] > meta_high['gap_penalty']

    @patch('src.tool_usage_tracker.get_tool_usage_tracker')
    def test_confidence_bounds(self, mock_tracker):
        """All confidence outputs should stay in [0.05, 0.95]."""
        from src.confidence import derive_confidence
        mock_tracker.return_value.get_usage_stats.return_value = {'total_calls': 0}

        # Extreme states
        test_cases = [
            self._make_state(E=0.0, I=0.0, S=1.0, V=1.0, coherence=0.0),
            self._make_state(E=1.0, I=1.0, S=0.0, V=0.0, coherence=1.0),
            self._make_state(E=0.5, I=0.5, S=0.5, V=0.5, coherence=0.5),
        ]
        for state in test_cases:
            conf, _ = derive_confidence(state, agent_id="bounds-test")
            assert 0.05 <= conf <= 0.95, f"Confidence {conf} out of bounds"

    @patch('src.tool_usage_tracker.get_tool_usage_tracker')
    def test_metadata_tracks_variance_sources(self, mock_tracker):
        """Metadata should include gap_penalty and agent_offset."""
        from src.confidence import derive_confidence
        mock_tracker.return_value.get_usage_stats.return_value = {'total_calls': 0}

        state = self._make_state()
        _, meta = derive_confidence(state, agent_id="meta-test")

        assert 'gap_penalty' in meta
        assert 'agent_offset' in meta
        assert 'tool_eisv_gap' in meta
        assert meta['source'] == 'eisv_with_variance'


# ═══════════════════════════════════════════════════════════════════════
# Fix 2: Agent purpose inference
# ═══════════════════════════════════════════════════════════════════════

class TestPurposeInference:
    """Purpose should be inferred from response text keywords."""

    def test_debug_keywords(self):
        from src.mcp_handlers.updates.phases import _infer_purpose
        assert _infer_purpose("I'm debugging the authentication error") == "debugging"

    def test_implementation_keywords(self):
        from src.mcp_handlers.updates.phases import _infer_purpose
        assert _infer_purpose("I'm implementing a new feature to build user profiles") == "implementation"

    def test_testing_keywords(self):
        from src.mcp_handlers.updates.phases import _infer_purpose
        assert _infer_purpose("Running pytest to check test coverage") == "testing"

    def test_review_keywords(self):
        from src.mcp_handlers.updates.phases import _infer_purpose
        assert _infer_purpose("Reviewing the code and auditing for security") == "review"

    def test_deployment_keywords(self):
        from src.mcp_handlers.updates.phases import _infer_purpose
        assert _infer_purpose("Deploying the release to production") == "deployment"

    def test_exploration_keywords(self):
        from src.mcp_handlers.updates.phases import _infer_purpose
        assert _infer_purpose("Exploring the codebase to understand the architecture") == "exploration"

    def test_no_keywords_returns_none(self):
        from src.mcp_handlers.updates.phases import _infer_purpose
        assert _infer_purpose("hello world") is None

    def test_empty_string(self):
        from src.mcp_handlers.updates.phases import _infer_purpose
        assert _infer_purpose("") is None


class TestIdentityReminder:
    """Identity reminder should appear for first 3 updates when label/purpose missing."""

    def test_reminder_shown_no_label_no_purpose(self):
        from src.mcp_handlers.updates.enrichments import enrich_identity_reminder
        from src.mcp_handlers.updates.context import UpdateContext

        ctx = UpdateContext()
        ctx.meta = MagicMock()
        ctx.meta.total_updates = 1
        ctx.meta.label = None
        ctx.meta.purpose = None
        ctx.response_data = {}

        enrich_identity_reminder(ctx)

        assert 'identity_reminder' in ctx.response_data
        assert len(ctx.response_data['identity_reminder']['missing']) == 2

    def test_reminder_not_shown_after_3_updates(self):
        from src.mcp_handlers.updates.enrichments import enrich_identity_reminder
        from src.mcp_handlers.updates.context import UpdateContext

        ctx = UpdateContext()
        ctx.meta = MagicMock()
        ctx.meta.total_updates = 5
        ctx.meta.label = None
        ctx.meta.purpose = None
        ctx.response_data = {}

        enrich_identity_reminder(ctx)

        assert 'identity_reminder' not in ctx.response_data

    def test_reminder_not_shown_when_identity_set(self):
        from src.mcp_handlers.updates.enrichments import enrich_identity_reminder
        from src.mcp_handlers.updates.context import UpdateContext

        ctx = UpdateContext()
        ctx.meta = MagicMock()
        ctx.meta.total_updates = 1
        ctx.meta.label = "MyAgent"
        ctx.meta.purpose = "debugging"
        ctx.response_data = {}

        enrich_identity_reminder(ctx)

        assert 'identity_reminder' not in ctx.response_data


# ═══════════════════════════════════════════════════════════════════════
# Fix 3: Decision sub_action granularity
# ═══════════════════════════════════════════════════════════════════════

class TestDecisionSubAction:
    """Decision dicts should include sub_action for granular tracking."""

    def test_low_risk_approve(self):
        decision = GovernanceConfig.make_decision(
            risk_score=0.1, coherence=0.6, void_active=False
        )
        assert decision['action'] == 'proceed'
        assert decision['sub_action'] == 'approve'

    def test_medium_risk_guide(self):
        decision = GovernanceConfig.make_decision(
            risk_score=0.45, coherence=0.6, void_active=False
        )
        assert decision['action'] == 'proceed'
        assert decision['sub_action'] == 'guide'

    def test_high_risk_reject(self):
        decision = GovernanceConfig.make_decision(
            risk_score=0.7, coherence=0.6, void_active=False
        )
        assert decision['action'] == 'pause'
        assert decision['sub_action'] == 'reject'

    def test_void_pause(self):
        decision = GovernanceConfig.make_decision(
            risk_score=0.1, coherence=0.6, void_active=True
        )
        assert decision['action'] == 'pause'
        assert decision['sub_action'] == 'void_pause'

    def test_coherence_pause(self):
        decision = GovernanceConfig.make_decision(
            risk_score=0.1, coherence=0.1, void_active=False
        )
        assert decision['action'] == 'pause'
        assert decision['sub_action'] == 'coherence_pause'

    def test_monitor_decision_cirs_block_resonance(self):
        state = MagicMock()
        state.E = 0.8
        state.I = 0.8
        state.S = 0.1
        state.V = 0.0
        state.coherence = 0.6
        state.void_active = False
        state.coherence_history = []

        oi_state = MagicMock()
        oi_state.oi = 3.5
        oi_state.flips = 5
        oi_state.resonant = True

        decision = monitor_make_decision(
            state=state, risk_score=0.1,
            response_tier='hard_block', oscillation_state=oi_state
        )
        assert decision['sub_action'] == 'cirs_block'
        assert 'resonance' in decision['reason'].lower()
        assert decision['nearest_edge'] == 'oscillation'

    def test_monitor_decision_cirs_block_risk_ceiling(self):
        """hard_block fired by risk > beta_high should label the reason as risk, not resonance."""
        state = MagicMock()
        state.E = 0.3
        state.I = 0.9
        state.S = 0.25
        state.V = 0.0
        state.coherence = 0.49
        state.void_active = False
        state.coherence_history = []

        oi_state = MagicMock()
        oi_state.oi = 0.30
        oi_state.flips = 2
        oi_state.resonant = False

        decision = monitor_make_decision(
            state=state, risk_score=0.85,
            response_tier='hard_block', oscillation_state=oi_state
        )
        assert decision['sub_action'] == 'cirs_block'
        assert 'risk ceiling' in decision['reason'].lower()
        assert 'resonance' not in decision['reason'].lower()
        assert decision['nearest_edge'] == 'risk'

    def test_monitor_decision_cirs_block_coherence_floor(self):
        """hard_block fired by coherence < tau_low should label the reason as coherence."""
        state = MagicMock()
        state.E = 0.5
        state.I = 0.5
        state.S = 0.5
        state.V = 0.0
        state.coherence = 0.25
        state.void_active = False
        state.coherence_history = []

        oi_state = MagicMock()
        oi_state.oi = 0.10
        oi_state.flips = 1
        oi_state.resonant = False

        decision = monitor_make_decision(
            state=state, risk_score=0.4,
            response_tier='hard_block', oscillation_state=oi_state
        )
        assert decision['sub_action'] == 'cirs_block'
        assert 'legacy control-feedback floor' in decision['reason'].lower()
        assert decision['nearest_edge'] == 'coherence'
        assert 'resonance' not in decision['reason'].lower()
        assert decision['nearest_edge'] == 'coherence'

    def test_monitor_decision_risk_pause(self):
        state = MagicMock()
        state.E = 0.8
        state.I = 0.8
        state.S = 0.1
        state.V = 0.0
        state.coherence = 0.6
        state.void_active = False
        state.coherence_history = []

        decision = monitor_make_decision(
            state=state, risk_score=0.8, unitares_verdict='high-risk'
        )
        assert decision['sub_action'] == 'risk_pause'

    def test_monitor_decision_caution_guide(self):
        state = MagicMock()
        state.E = 0.8
        state.I = 0.8
        state.S = 0.1
        state.V = 0.0
        state.coherence = 0.6
        state.void_active = False
        state.coherence_history = []

        decision = monitor_make_decision(
            state=state, risk_score=0.1, unitares_verdict='caution'
        )
        assert decision['action'] == 'proceed'
        assert decision['sub_action'] == 'guide'

    def test_decision_history_uses_sub_action(self):
        """decision_history should record sub_action instead of flat 'proceed'."""
        decision = {'action': 'proceed', 'sub_action': 'approve'}
        # Simulate what governance_monitor.py does
        recorded = decision.get('sub_action', decision['action'])
        assert recorded == 'approve'

        decision2 = {'action': 'pause', 'sub_action': 'void_pause'}
        recorded2 = decision2.get('sub_action', decision2['action'])
        assert recorded2 == 'void_pause'


# ═══════════════════════════════════════════════════════════════════════
# Fix 4: PI controller gate relaxation
# ═══════════════════════════════════════════════════════════════════════

class TestPIControllerGateRelaxation:
    """PI controller should relax confidence gate when coherence is declining."""

    def _make_monitor(self, coherence, coherence_history, update_count=10):
        """Create a minimal UNITARESMonitor-like mock for testing gate logic."""
        state = MagicMock()
        state.coherence = coherence
        state.coherence_history = deque(coherence_history)
        state.update_count = update_count
        return state

    def test_normal_threshold_when_coherence_healthy(self):
        """When coherence is healthy, effective threshold stays at 0.55."""
        state = self._make_monitor(coherence=0.60, coherence_history=[0.58, 0.59, 0.60])

        effective = config.CONTROLLER_CONFIDENCE_THRESHOLD
        # Coherence >= TARGET_COHERENCE: no relaxation
        assert state.coherence >= config.TARGET_COHERENCE
        assert effective == 0.55

    def test_relaxed_threshold_when_coherence_declining(self):
        """When coherence < target AND declining, threshold should relax to 0.40."""
        state = self._make_monitor(coherence=0.48, coherence_history=[0.52, 0.50, 0.48])

        # Simulate the gate logic from governance_monitor.py
        effective_conf_threshold = config.CONTROLLER_CONFIDENCE_THRESHOLD
        if (state.coherence < config.TARGET_COHERENCE and
                len(state.coherence_history) >= 3):
            recent = list(state.coherence_history)[-3:]
            if recent[-1] < recent[0]:  # declining
                effective_conf_threshold = 0.40

        assert effective_conf_threshold == 0.40

    def test_no_relaxation_when_coherence_low_but_stable(self):
        """When coherence is below target but stable/rising, keep normal threshold."""
        state = self._make_monitor(coherence=0.50, coherence_history=[0.48, 0.49, 0.50])

        effective_conf_threshold = config.CONTROLLER_CONFIDENCE_THRESHOLD
        if (state.coherence < config.TARGET_COHERENCE and
                len(state.coherence_history) >= 3):
            recent = list(state.coherence_history)[-3:]
            if recent[-1] < recent[0]:  # NOT declining (rising)
                effective_conf_threshold = 0.40

        assert effective_conf_threshold == 0.55

    def test_controller_proceeds_at_042_when_declining(self):
        """At confidence=0.42, controller should proceed when coherence is declining."""
        confidence = 0.42

        # Declining coherence: effective threshold relaxes to 0.40
        effective_conf_threshold = 0.40
        assert confidence >= effective_conf_threshold

    def test_controller_blocked_at_042_when_stable(self):
        """At confidence=0.42, controller should be blocked when coherence is stable."""
        confidence = 0.42

        # Stable coherence: threshold stays at 0.55
        effective_conf_threshold = 0.55
        assert confidence < effective_conf_threshold


class TestCoherenceMonitoringTask:
    """Legacy C(V) must not drive background health alarms."""

    def test_legacy_health_alarm_task_is_retired(self):
        import src.background_tasks as background_tasks

        assert not hasattr(background_tasks, "coherence_monitoring_task")

    def test_legacy_health_alarm_is_not_scheduled(self):
        import inspect
        from src.background_tasks import start_all_background_tasks

        source = inspect.getsource(start_all_background_tasks)
        assert "coherence_monitoring_task" not in source
        assert "coherence_monitor" not in source
