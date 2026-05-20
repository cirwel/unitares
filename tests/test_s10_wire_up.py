"""S10.2 wire-up tests: class_tag flows from agent_metadata cache through
outcome_events.py into the sequential calibration tracker; check_calibration
MCP response carries the by_class envelope; class_promotion_sweeper_task
rebuckets the tracker.
"""

import sys
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from tests.helpers import parse_result


# ============================================================================
# outcome_events.py:332 — class_tag is fetched and passed through
# ============================================================================

class TestOutcomeEventsClassTagWireUp:
    """outcome_events resolves class_tag via classify_agent(agent_metadata[agent_id])
    and passes it to record_exogenous_tactical_outcome. Cache miss falls through
    to classify_agent(None) == "default". Exceptions in the lookup degrade
    gracefully to class_tag=None (UNKNOWN_CLASS_BUCKET in the tracker)."""

    @pytest.mark.asyncio
    async def test_class_tag_resolved_from_cached_meta(self):
        """When agent_metadata has the agent with tags, classify_agent's
        result is forwarded to the tracker."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-class')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })
        mock_checker = MagicMock()
        mock_seq_tracker = MagicMock()

        # AgentMetadata-shape stub: classify_agent reads .tags via getattr.
        cached_meta = SimpleNamespace(tags=['engaged_ephemeral'], label='alice')

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-engaged'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker), \
             patch.dict('src.agent_metadata_model.agent_metadata', {'agent-engaged': cached_meta}, clear=False):

            mock_server.monitors = {}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            await handle_outcome_event({
                'outcome_type': 'test_passed',
                'confidence': 0.9,
            })

        mock_seq_tracker.record_exogenous_tactical_outcome.assert_called_once()
        kwargs = mock_seq_tracker.record_exogenous_tactical_outcome.call_args[1]
        assert kwargs['class_tag'] == 'engaged_ephemeral'
        assert kwargs['agent_id'] == 'agent-engaged'

    @pytest.mark.asyncio
    async def test_class_tag_defaults_on_cache_miss(self):
        """When agent_metadata has no entry for agent_id, classify_agent(None)
        returns "default" — that string is forwarded to the tracker. The
        write must not skip just because class lookup misses."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-miss')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })
        mock_checker = MagicMock()
        mock_seq_tracker = MagicMock()

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-missing'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker), \
             patch.dict('src.agent_metadata_model.agent_metadata', {}, clear=True):

            mock_server.monitors = {}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            await handle_outcome_event({
                'outcome_type': 'test_passed',
                'confidence': 0.9,
            })

        mock_seq_tracker.record_exogenous_tactical_outcome.assert_called_once()
        kwargs = mock_seq_tracker.record_exogenous_tactical_outcome.call_args[1]
        assert kwargs['class_tag'] == 'default'

    @pytest.mark.asyncio
    async def test_class_lookup_exception_degrades_to_none(self):
        """If the class lookup itself raises, class_tag falls through to None
        and the tracker write still fires. The calibration data must not be
        lost because the class resolver had a transient failure."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-err')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })
        mock_checker = MagicMock()
        mock_seq_tracker = MagicMock()

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-raise'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker), \
             patch('src.grounding.class_indicator.classify_agent',
                   side_effect=RuntimeError("boom")):

            mock_server.monitors = {}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            await handle_outcome_event({
                'outcome_type': 'test_passed',
                'confidence': 0.9,
            })

        mock_seq_tracker.record_exogenous_tactical_outcome.assert_called_once()
        kwargs = mock_seq_tracker.record_exogenous_tactical_outcome.call_args[1]
        assert kwargs['class_tag'] is None


# ============================================================================
# admin/calibration.py — by_class envelope on check_calibration response
# ============================================================================

class TestCheckCalibrationByClassEnvelope:
    """The check_calibration MCP response should carry a by_class envelope
    with bootstrapped + buckets keys, derived from the tracker."""

    @pytest.mark.asyncio
    async def test_by_class_envelope_present_in_response(self, tmp_path):
        """End-to-end: write a few outcomes with different class_tags, call
        handle_check_calibration, assert by_class envelope is in the
        response and reflects the writes."""
        from src.sequential_calibration import SequentialCalibrationTracker

        # Drive a fresh tracker so we can control state precisely.
        tracker = SequentialCalibrationTracker(state_file=tmp_path / "seq.json")
        for _ in range(2):
            tracker.record_exogenous_tactical_outcome(
                confidence=0.9, outcome_correct=True,
                agent_id="a", class_tag="substrate",
                signal_source="tests",
            )
        for _ in range(3):
            tracker.record_exogenous_tactical_outcome(
                confidence=0.7, outcome_correct=False,
                agent_id="b", class_tag="ephemeral",
                signal_source="tests",
            )

        mock_checker = MagicMock()
        # Minimal check_calibration return shape.
        mock_checker.check_calibration.return_value = (
            True,
            {"bins": {}, "issues": [], "per_channel_calibration": {}},
        )
        mock_checker.get_pending_updates.return_value = []

        with patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.get_sequential_calibration_tracker',
                   return_value=tracker):

            from src.mcp_handlers.admin.calibration import handle_check_calibration
            result = await handle_check_calibration({})

        parsed = parse_result(result)
        assert "by_class" in parsed
        envelope = parsed["by_class"]
        assert envelope["bootstrapped"] is True
        assert "substrate" in envelope["by_class"]
        assert "ephemeral" in envelope["by_class"]
        assert envelope["by_class"]["substrate"]["eligible_samples"] == 2
        assert envelope["by_class"]["ephemeral"]["eligible_samples"] == 3
        # E-process fields stay off the class envelope (S10 council finding).
        assert "log_evidence" not in envelope["by_class"]["substrate"]
        assert "capped_alarm" not in envelope["by_class"]["substrate"]

    @pytest.mark.asyncio
    async def test_response_survives_tracker_error(self):
        """If the tracker raises during compute_metrics_by_class, the response
        still succeeds — by_class is best-effort, not load-bearing."""
        mock_checker = MagicMock()
        mock_checker.check_calibration.return_value = (
            True, {"bins": {}, "issues": [], "per_channel_calibration": {}},
        )
        mock_checker.get_pending_updates.return_value = []

        broken_tracker = MagicMock()
        broken_tracker.compute_metrics_by_class.side_effect = RuntimeError("nope")
        # The handler also reads compute_metrics() for the tactical_evidence
        # block earlier in the response build; let that be quiet.
        broken_tracker.compute_metrics.return_value = {}

        with patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.get_sequential_calibration_tracker',
                   return_value=broken_tracker):

            from src.mcp_handlers.admin.calibration import handle_check_calibration
            result = await handle_check_calibration({})

        parsed = parse_result(result)
        # Response must still be successful; by_class simply absent.
        assert parsed.get("calibration_status") in {"no_data", "calibrated", "miscalibrated", "signal_stale"}
        assert "by_class" not in parsed


# ============================================================================
# background_tasks.class_promotion_sweeper_task — rebucket hook
# ============================================================================

class TestSweeperRebucketHook:
    """The class_promotion_sweeper_task should call
    tracker.rebucket_from_agent_states each cycle, with a classifier closure
    that reads agent_metadata and routes through classify_agent."""

    @pytest.mark.asyncio
    async def test_sweeper_calls_rebucket_with_metadata_classifier(self):
        """Drive one cycle of the sweeper by patching asyncio.sleep to break
        the loop on the second sleep; assert tracker.rebucket_from_agent_states
        was invoked and the classifier resolves a known agent through the
        agent_metadata cache. Classifier must be exercised INSIDE the patched
        scope so agent_metadata.get sees the test entry."""
        from types import SimpleNamespace

        captured = {}

        def fake_rebucket(*, classifier, persist=True):
            # Exercise the classifier while agent_metadata is still patched
            # so the closure sees the test entry, not real empty cache.
            captured['known'] = classifier('agent-x')
            captured['unknown'] = classifier('agent-unknown')
            return {
                "tracked_agents": 1,
                "unresolved_agents": 0,
                "classifier_errors": 0,
                "buckets": {"engaged_ephemeral": 1},
            }

        tracker_stub = SimpleNamespace(rebucket_from_agent_states=fake_rebucket)

        call_count = {"n": 0}
        original_sleep = __import__("asyncio").sleep

        async def staged_sleep(delay):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise __import__("asyncio").CancelledError
            return await original_sleep(0)

        cached_meta = SimpleNamespace(tags=['engaged_ephemeral'])

        with patch('asyncio.sleep', side_effect=staged_sleep), \
             patch('src.grounding.class_promotion.promote_engaged_ephemeral',
                   new=AsyncMock(return_value={"promoted": 0, "threshold": 3})), \
             patch('src.sequential_calibration.get_sequential_calibration_tracker',
                   return_value=tracker_stub), \
             patch.dict('src.agent_metadata_model.agent_metadata',
                        {'agent-x': cached_meta}, clear=False):

            from src.background_tasks import class_promotion_sweeper_task
            await class_promotion_sweeper_task(interval_minutes=0.0)

        assert captured.get('known') == 'engaged_ephemeral', captured
        # Missing agents return None so the tracker buckets to UNKNOWN.
        assert captured.get('unknown') is None

    @pytest.mark.asyncio
    async def test_sweeper_rebucket_failure_does_not_abort(self):
        """A rebucket exception must not break the sweeper loop — log + retry."""
        from types import SimpleNamespace

        broken_tracker = SimpleNamespace(
            rebucket_from_agent_states=MagicMock(side_effect=RuntimeError("rebucket boom"))
        )

        call_count = {"n": 0}
        original_sleep = __import__("asyncio").sleep

        async def staged_sleep(delay):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise __import__("asyncio").CancelledError
            return await original_sleep(0)

        with patch('asyncio.sleep', side_effect=staged_sleep), \
             patch('src.grounding.class_promotion.promote_engaged_ephemeral',
                   new=AsyncMock(return_value={"promoted": 0, "threshold": 3})), \
             patch('src.sequential_calibration.get_sequential_calibration_tracker',
                   return_value=broken_tracker):

            from src.background_tasks import class_promotion_sweeper_task
            # Must not raise — the sweeper swallows rebucket exceptions.
            await class_promotion_sweeper_task(interval_minutes=0.0)

        broken_tracker.rebucket_from_agent_states.assert_called_once()
