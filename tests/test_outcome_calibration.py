"""
Tests for outcome event → calibration wiring.

Covers:
- Phase 5 auto-emit records calibration (positive + negative)
- Explicit outcome_event handler records calibration
- No double-emit (completion takes priority over failure)
- Tactical calibration for test outcomes
- Confidence lookup fallback from monitor
"""

import pytest
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from types import SimpleNamespace

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from mcp.types import TextContent
from tests.helpers import parse_result


# ============================================================================
# Phase 5: auto-emit calibration wiring
# ============================================================================

class TestPhase5CalibrationWiring:
    """Test calibration recording from Phase 5 auto-emitted outcome events."""

    @pytest.fixture
    def phase5_ctx(self):
        """Minimal ctx object for Phase 5 auto-emit path."""
        ctx = SimpleNamespace(
            response_text="Completed the feature implementation",
            complexity=0.5,
            confidence=0.8,
            arguments={'confidence': 0.8, 'client_session_id': 'sess-1'},
            metrics_dict={
                'E': 0.72, 'I': 0.75, 'S': 0.15, 'V': -0.03,
                'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
            },
            outcome_event_id=None,
            result=None,
        )
        return ctx

    @pytest.mark.asyncio
    async def test_positive_outcome_records_calibration(self, phase5_ctx):
        """Auto-emitted task_completed should record calibration prediction."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='outcome-123')

        mock_checker = MagicMock()
        mock_checker.record_prediction = MagicMock()

        agent_id = 'agent-abc'
        ctx = phase5_ctx

        # Simulate Phase 5 auto-emit logic inline
        ctx.outcome_event_id = None
        if ctx.response_text and ctx.complexity >= 0.3:
            _rt_lower = ctx.response_text.lower()
            _completion_signals = (
                'completed', 'implemented', 'deployed', 'finished',
                'fixed', 'resolved', 'shipped', 'merged', 'built',
                'created', 'added', 'refactored', 'migrated',
            )
            if any(sig in _rt_lower for sig in _completion_signals):
                ctx.outcome_event_id = await mock_db.record_outcome_event(
                    agent_id=agent_id,
                    outcome_type='task_completed',
                    is_bad=False,
                    outcome_score=min(1.0, ctx.metrics_dict.get('coherence', 0.5) * 1.5),
                    session_id=ctx.arguments.get('client_session_id'),
                    eisv_e=ctx.metrics_dict.get('E'),
                    eisv_i=ctx.metrics_dict.get('I'),
                    eisv_s=ctx.metrics_dict.get('S'),
                    eisv_v=ctx.metrics_dict.get('V'),
                    eisv_phi=ctx.metrics_dict.get('phi'),
                    eisv_verdict=ctx.metrics_dict.get('verdict'),
                    eisv_coherence=ctx.metrics_dict.get('coherence'),
                    eisv_regime=ctx.metrics_dict.get('regime'),
                    detail={
                        'source': 'auto_checkin',
                        'complexity': ctx.complexity,
                        'confidence': ctx.arguments.get('confidence'),
                        'summary': ctx.response_text[:500],
                    },
                )
                if ctx.outcome_event_id:
                    _conf = ctx.confidence
                    if _conf is not None:
                        _outcome_score = min(1.0, ctx.metrics_dict.get('coherence', 0.5) * 1.5)
                        mock_checker.record_prediction(
                            confidence=float(_conf),
                            predicted_correct=(float(_conf) >= 0.5),
                            actual_correct=_outcome_score,
                        )

        assert ctx.outcome_event_id == 'outcome-123'
        mock_checker.record_prediction.assert_called_once_with(
            confidence=0.8,
            predicted_correct=True,
            actual_correct=min(1.0, 0.48 * 1.5),
        )

    @pytest.mark.asyncio
    async def test_no_calibration_when_confidence_missing(self, phase5_ctx):
        """No calibration recorded when confidence is None."""
        phase5_ctx.arguments = {'client_session_id': 'sess-1'}  # No confidence
        phase5_ctx.confidence = None

        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='outcome-456')
        mock_checker = MagicMock()

        ctx = phase5_ctx
        ctx.outcome_event_id = None
        if ctx.response_text and ctx.complexity >= 0.3:
            _rt_lower = ctx.response_text.lower()
            if 'completed' in _rt_lower:
                ctx.outcome_event_id = await mock_db.record_outcome_event(
                    agent_id='agent-x', outcome_type='task_completed',
                    is_bad=False, outcome_score=0.72,
                    session_id=None, eisv_e=None, eisv_i=None, eisv_s=None,
                    eisv_v=None, eisv_phi=None, eisv_verdict=None,
                    eisv_coherence=None, eisv_regime=None, detail={},
                )
                if ctx.outcome_event_id:
                    _conf = ctx.confidence
                    if _conf is not None:
                        mock_checker.record_prediction(
                            confidence=float(_conf),
                            predicted_correct=(float(_conf) >= 0.5),
                            actual_correct=0.72,
                        )

        assert ctx.outcome_event_id == 'outcome-456'
        mock_checker.record_prediction.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_outcome_auto_emit(self, phase5_ctx):
        """Failure signals emit task_failed + calibration when no completion emitted."""
        phase5_ctx.response_text = "The build failed with regression errors"

        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='bad-outcome-1')
        mock_checker = MagicMock()

        ctx = phase5_ctx
        agent_id = 'agent-fail'
        ctx.outcome_event_id = None

        if ctx.response_text and ctx.complexity >= 0.3:
            _rt_lower = ctx.response_text.lower()
            _completion_signals = (
                'completed', 'implemented', 'deployed', 'finished',
                'fixed', 'resolved', 'shipped', 'merged', 'built',
                'created', 'added', 'refactored', 'migrated',
            )
            if any(sig in _rt_lower for sig in _completion_signals):
                ctx.outcome_event_id = 'should-not-reach'

            # Negative outcome auto-emit
            if not ctx.outcome_event_id:
                _failure_signals = (
                    'failed', 'error', 'broken', 'reverted', 'blocked',
                    'stuck', 'crash', 'regression',
                )
                if any(sig in _rt_lower for sig in _failure_signals):
                    _bad_score = max(0.0, 1.0 - ctx.metrics_dict.get('coherence', 0.5) * 1.5)
                    _bad_oid = await mock_db.record_outcome_event(
                        agent_id=agent_id,
                        outcome_type='task_failed',
                        is_bad=True,
                        outcome_score=_bad_score,
                        session_id=ctx.arguments.get('client_session_id'),
                        eisv_e=ctx.metrics_dict.get('E'),
                        eisv_i=ctx.metrics_dict.get('I'),
                        eisv_s=ctx.metrics_dict.get('S'),
                        eisv_v=ctx.metrics_dict.get('V'),
                        eisv_phi=ctx.metrics_dict.get('phi'),
                        eisv_verdict=ctx.metrics_dict.get('verdict'),
                        eisv_coherence=ctx.metrics_dict.get('coherence'),
                        eisv_regime=ctx.metrics_dict.get('regime'),
                        detail={
                            'source': 'auto_checkin',
                            'complexity': ctx.complexity,
                            'confidence': ctx.arguments.get('confidence'),
                            'summary': ctx.response_text[:500],
                            'is_negative': True,
                        },
                    )
                    if _bad_oid:
                        _conf = ctx.confidence
                        if _conf is not None:
                            mock_checker.record_prediction(
                                confidence=float(_conf),
                                predicted_correct=(float(_conf) >= 0.5),
                                actual_correct=_bad_score,
                            )

        assert ctx.outcome_event_id is None  # No completion signal matched
        mock_db.record_outcome_event.assert_called_once()
        call_kwargs = mock_db.record_outcome_event.call_args
        assert call_kwargs.kwargs['outcome_type'] == 'task_failed'
        assert call_kwargs.kwargs['is_bad'] is True

        expected_bad_score = max(0.0, 1.0 - 0.48 * 1.5)
        mock_checker.record_prediction.assert_called_once_with(
            confidence=0.8,
            predicted_correct=True,
            actual_correct=expected_bad_score,
        )

    @pytest.mark.asyncio
    async def test_no_double_emit_completion_takes_priority(self, phase5_ctx):
        """When text has both 'fixed' and 'error', completion wins — no negative emit."""
        phase5_ctx.response_text = "Fixed the error in the parser"

        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='outcome-fix')
        mock_checker = MagicMock()

        ctx = phase5_ctx
        ctx.outcome_event_id = None

        if ctx.response_text and ctx.complexity >= 0.3:
            _rt_lower = ctx.response_text.lower()
            _completion_signals = ('completed', 'implemented', 'deployed', 'finished',
                                   'fixed', 'resolved', 'shipped', 'merged', 'built',
                                   'created', 'added', 'refactored', 'migrated')
            if any(sig in _rt_lower for sig in _completion_signals):
                ctx.outcome_event_id = await mock_db.record_outcome_event(
                    agent_id='agent-x', outcome_type='task_completed',
                    is_bad=False, outcome_score=0.72,
                    session_id=None, eisv_e=None, eisv_i=None, eisv_s=None,
                    eisv_v=None, eisv_phi=None, eisv_verdict=None,
                    eisv_coherence=None, eisv_regime=None, detail={},
                )

            # Negative check — should NOT fire because outcome_event_id is set
            if not ctx.outcome_event_id:
                _failure_signals = ('failed', 'error', 'broken', 'reverted', 'blocked',
                                    'stuck', 'crash', 'regression')
                if any(sig in _rt_lower for sig in _failure_signals):
                    pytest.fail("Should not reach negative emit when completion matched")

        assert ctx.outcome_event_id == 'outcome-fix'
        # Only one call (for completion), not two
        mock_db.record_outcome_event.assert_called_once()


# ============================================================================
# Explicit outcome_event handler: calibration wiring
# ============================================================================

class TestExplicitOutcomeEventCalibration:
    """Test calibration recording from explicit outcome_event calls."""

    @pytest.mark.asyncio
    async def test_outcome_event_persists_stable_session_and_split_snapshot(self):
        """outcome_event should keep stable session linkage and attach split EISV semantics."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-split')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })

        mock_monitor = MagicMock()
        mock_monitor._behavioral_state = SimpleNamespace(
            E=0.51, I=0.68, S=0.22, V=-0.11, confidence=0.45,
        )
        mock_monitor._prev_confidence = None
        mock_monitor.get_primary_eisv.return_value = (0.51, 0.68, 0.22, -0.11)

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-split'), \
             patch('src.mcp_handlers.context.get_context_client_session_id', return_value='agent-split-123'):

            mock_server.monitors = {'agent-split': mock_monitor}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'task_completed',
            })

        parsed = parse_result(result)
        assert parsed.get('outcome_id') == 'oe-split'
        assert parsed['eisv_snapshot']['primary_eisv_source'] == 'behavioral'
        assert parsed['eisv_snapshot']['primary_eisv']['E'] == 0.51
        assert parsed['eisv_snapshot']['behavioral_eisv']['E'] == 0.51
        assert parsed['eisv_snapshot']['ode_eisv']['E'] == 0.7
        assert parsed['eisv_snapshot']['state_semantics']['flat_fields_mean'] == 'primary_eisv'

        _, kwargs = mock_db.record_outcome_event.call_args
        assert kwargs['session_id'] == 'agent-split-123'
        assert kwargs['detail']['snapshot_source'] == 'latest_agent_state'
        assert kwargs['detail']['snapshot_missing'] is False
        assert kwargs['detail']['primary_eisv_source'] == 'behavioral'
        assert kwargs['detail']['behavioral_eisv']['E'] == 0.51
        assert kwargs['detail']['ode_eisv']['E'] == 0.7
        assert kwargs['detail']['reported_confidence'] is None
        assert kwargs['detail']['eprocess_eligible'] is False

    @pytest.mark.asyncio
    async def test_records_calibration_with_explicit_confidence(self):
        """outcome_event with confidence param records calibration."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-1')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })

        mock_checker = MagicMock()
        mock_checker.record_prediction = MagicMock()
        mock_checker.record_tactical_decision = MagicMock()
        mock_seq_tracker = MagicMock()

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-test'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker):

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                'confidence': 0.85,
            })

        parsed = parse_result(result)
        assert parsed.get('outcome_id') == 'oe-1'

        # Should have recorded both prediction and tactical
        mock_checker.record_prediction.assert_called_once_with(
            confidence=0.85,
            predicted_correct=True,
            actual_correct=1.0,  # test_passed → outcome_score=1.0
        )
        mock_checker.record_tactical_decision.assert_called_once_with(
            confidence=0.85,
            decision='proceed',
            immediate_outcome=True,  # not is_bad
            signal_source='tests',  # routes to per-channel breakdown
        )
        mock_seq_tracker.record_exogenous_tactical_outcome.assert_called_once_with(
            confidence=0.85,
            outcome_correct=True,
            agent_id='agent-test',
            class_tag='default',  # S10.2: classify_agent(None)="default" when agent_metadata cache miss
            signal_source='tests',
            decision_action='proceed',
            outcome_type='test_passed',
            prediction_id=None,
        )
        _, kwargs = mock_db.record_outcome_event.call_args
        assert kwargs['detail']['hard_exogenous_signal'] == 'tests'
        assert kwargs['detail']['eprocess_eligible'] is True
        assert kwargs['detail']['reported_confidence'] == 0.85

    @pytest.mark.asyncio
    async def test_confidence_fallback_from_monitor(self):
        """When confidence not in args, falls back to monitor._prev_confidence."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-2')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })

        mock_monitor = MagicMock()
        mock_monitor._prev_confidence = 0.7

        mock_checker = MagicMock()
        mock_seq_tracker = MagicMock()

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-mon'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker):

            mock_server.monitors = {'agent-mon': mock_monitor}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'task_completed',
                # No confidence param
            })

        parsed = parse_result(result)
        assert parsed.get('outcome_id') == 'oe-2'
        assert parsed['prediction_binding'] == 'prev_confidence_fallback'

        mock_checker.record_prediction.assert_called_once_with(
            confidence=0.7,
            predicted_correct=True,
            actual_correct=1.0,
        )
        # task_completed became hard-exogenous-eligible when the truth channel
        # broadened (epoch 3); the seq_tracker is now called for it.
        mock_seq_tracker.record_exogenous_tactical_outcome.assert_called_once_with(
            confidence=0.7,
            outcome_correct=True,
            agent_id='agent-mon',
            class_tag='default',
            signal_source='tasks',
            decision_action=None,
            outcome_type='task_completed',
            prediction_id=None,
        )
        _, kwargs = mock_db.record_outcome_event.call_args
        assert kwargs['detail']['reported_confidence'] == 0.7
        assert kwargs['detail']['eprocess_eligible'] is True

    @pytest.mark.asyncio
    async def test_no_calibration_when_no_confidence_available(self):
        """No calibration when neither param, monitor, nor DB has confidence."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-3')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })
        mock_db.get_latest_confidence_before = AsyncMock(return_value=None)

        mock_checker = MagicMock()
        mock_seq_tracker = MagicMock()

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-no-conf'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker):

            mock_server.monitors = {}  # No monitor for this agent

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'task_completed',
            })

        parsed = parse_result(result)
        assert parsed.get('outcome_id') == 'oe-3'
        mock_checker.record_prediction.assert_not_called()
        mock_seq_tracker.record_exogenous_tactical_outcome.assert_not_called()

    @pytest.mark.asyncio
    async def test_confidence_fallback_from_audit_trail(self):
        """When no monitor confidence, falls back to DB audit trail."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-db')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })
        mock_db.get_latest_confidence_before = AsyncMock(return_value=0.65)

        mock_checker = MagicMock()
        mock_seq_tracker = MagicMock()

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-rest'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker):

            mock_server.monitors = {}  # No monitor — REST caller

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                'detail': {'tests': {'passed': 10, 'failed': 0}},
            })

        parsed = parse_result(result)
        assert parsed.get('outcome_id') == 'oe-db'
        assert parsed['prediction_binding'] == 'audit_trail_fallback'

        # Calibration should fire with DB-resolved confidence
        mock_checker.record_prediction.assert_called_once_with(
            confidence=0.65,
            predicted_correct=True,
            actual_correct=1.0,
        )

        # Sequential tracker should fire — test_passed is hard exogenous
        mock_seq_tracker.record_exogenous_tactical_outcome.assert_called_once()
        seq_kwargs = mock_seq_tracker.record_exogenous_tactical_outcome.call_args[1]
        assert seq_kwargs['confidence'] == 0.65
        assert seq_kwargs['outcome_correct'] is True
        assert seq_kwargs['signal_source'] == 'tests'

        # Verify detail records the source
        _, db_kwargs = mock_db.record_outcome_event.call_args
        assert db_kwargs['detail']['reported_confidence'] == 0.65
        assert db_kwargs['detail']['prediction_source'] == 'audit_trail_fallback'
        assert db_kwargs['detail']['eprocess_eligible'] is True

    @pytest.mark.asyncio
    async def test_test_failed_records_tactical_with_bad_outcome(self):
        """test_failed records tactical calibration with immediate_outcome=False."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-4')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })

        mock_checker = MagicMock()
        mock_seq_tracker = MagicMock()

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-tf'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker):

            mock_server.monitors = {}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_failed',
                'confidence': 0.9,  # Overconfident!
            })

        mock_checker.record_prediction.assert_called_once_with(
            confidence=0.9,
            predicted_correct=True,
            actual_correct=0.0,  # test_failed → is_bad=True → outcome_score=0.0
        )
        mock_checker.record_tactical_decision.assert_called_once_with(
            confidence=0.9,
            decision='proceed',
            immediate_outcome=False,  # is_bad=True → not is_bad = False
            signal_source='tests',  # routes to per-channel breakdown
        )
        mock_seq_tracker.record_exogenous_tactical_outcome.assert_called_once_with(
            confidence=0.9,
            outcome_correct=False,
            agent_id='agent-tf',
            class_tag='default',
            signal_source='tests',
            decision_action='proceed',
            outcome_type='test_failed',
            prediction_id=None,
        )


# ============================================================================
# Schema: OutcomeEventParams confidence field
# ============================================================================

class TestOutcomeEventParamsSchema:
    """Verify confidence field exists in OutcomeEventParams."""

    def test_confidence_field_exists(self):
        from src.mcp_handlers.schemas.core import OutcomeEventParams
        fields = OutcomeEventParams.model_fields
        assert 'confidence' in fields
        field_info = fields['confidence']
        assert field_info.default is None  # Optional

    def test_confidence_accepts_valid_value(self):
        from src.mcp_handlers.schemas.core import OutcomeEventParams
        params = OutcomeEventParams(outcome_type='test_passed', confidence=0.85)
        assert params.confidence == 0.85

    def test_confidence_none_by_default(self):
        from src.mcp_handlers.schemas.core import OutcomeEventParams
        params = OutcomeEventParams(outcome_type='test_passed')
        assert params.confidence is None

    def test_prediction_id_field_exists(self):
        from src.mcp_handlers.schemas.core import OutcomeEventParams
        fields = OutcomeEventParams.model_fields
        assert 'prediction_id' in fields
        assert fields['prediction_id'].default is None

    def test_prediction_id_accepts_string(self):
        from src.mcp_handlers.schemas.core import OutcomeEventParams
        params = OutcomeEventParams(outcome_type='test_passed', prediction_id='pid-abc')
        assert params.prediction_id == 'pid-abc'


# ============================================================================
# Phase-one seam: prediction_id lookup on outcome_event
# ============================================================================

class TestPredictionIdLookup:
    """outcome_event should prefer the registered confidence when prediction_id is provided."""

    @pytest.mark.asyncio
    async def test_prediction_id_uses_registered_confidence(self):
        """When prediction_id is present, registered confidence wins over _prev_confidence."""
        from src.monitor_prediction import register_tactical_prediction
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-pid-1')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })

        # Use a real _open_predictions dict so the two-phase lookup works correctly.
        open_predictions = {}
        pid = register_tactical_prediction(open_predictions, confidence=0.9)

        mock_monitor = MagicMock()
        # _prev_confidence is intentionally different to prove the registry path wins
        mock_monitor._prev_confidence = 0.4
        mock_monitor._open_predictions = open_predictions
        mock_monitor._prediction_ttl_seconds = 3600.0
        mock_monitor._behavioral_state = None
        mock_monitor.get_primary_eisv.return_value = (0.7, 0.75, 0.15, -0.03)

        mock_checker = MagicMock()
        mock_seq_tracker = MagicMock()

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-pid'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker):

            mock_server.monitors = {'agent-pid': mock_monitor}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                'prediction_id': pid,
            })

        parsed = parse_result(result)
        assert parsed.get('outcome_id') == 'oe-pid-1'

        # Registry was consumed (two-phase path)
        assert open_predictions[pid].get('consumed') is True

        # Calibration used the registered confidence (0.9), not _prev_confidence (0.4)
        mock_checker.record_prediction.assert_called_once_with(
            confidence=0.9,
            predicted_correct=True,
            actual_correct=1.0,
        )

        # Sequential tracker received prediction_id for audit
        mock_seq_tracker.record_exogenous_tactical_outcome.assert_called_once()
        _, seq_kwargs = mock_seq_tracker.record_exogenous_tactical_outcome.call_args
        assert seq_kwargs['confidence'] == 0.9
        assert seq_kwargs['prediction_id'] == pid
        assert seq_kwargs['signal_source'] == 'tests'

        # Detail preserves provenance
        _, db_kwargs = mock_db.record_outcome_event.call_args
        assert db_kwargs['detail']['reported_confidence'] == 0.9
        assert db_kwargs['detail']['prediction_id'] == pid
        assert db_kwargs['detail']['prediction_source'] == 'registry'
        assert db_kwargs['detail']['eprocess_eligible'] is True

    @pytest.mark.asyncio
    async def test_unknown_prediction_id_falls_back_to_prev_confidence(self):
        """If prediction_id is unknown to the monitor, fall back to _prev_confidence."""
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value='oe-pid-2')
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })

        mock_monitor = MagicMock()
        mock_monitor._prev_confidence = 0.55
        # Use a real empty dict so lookup_prediction correctly returns None for unknown ids
        mock_monitor._open_predictions = {}
        mock_monitor._prediction_ttl_seconds = 3600.0
        mock_monitor._behavioral_state = None

        mock_checker = MagicMock()
        mock_seq_tracker = MagicMock()

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-unknown-pid'), \
             patch('src.calibration.calibration_checker', mock_checker), \
             patch('src.sequential_calibration.sequential_calibration_tracker', mock_seq_tracker):

            mock_server.monitors = {'agent-unknown-pid': mock_monitor}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'task_completed',
                'prediction_id': 'pid-stale',
            })

        parsed = parse_result(result)
        assert parsed.get('outcome_id') == 'oe-pid-2'

        # Fallback confidence used (id was not in registry)
        mock_checker.record_prediction.assert_called_once_with(
            confidence=0.55,
            predicted_correct=True,
            actual_correct=1.0,
        )
        _, db_kwargs = mock_db.record_outcome_event.call_args
        assert db_kwargs['detail']['reported_confidence'] == 0.55
        assert db_kwargs['detail']['prediction_source'] == 'prev_confidence_fallback'
        assert db_kwargs['detail']['prediction_id'] == 'pid-stale'


# ============================================================================
# Task 1: Hard TTL on consume_prediction
# ============================================================================

class TestConsumePredictionHardTTL:
    """consume_prediction must enforce TTL at consume time, not just at registration."""

    def test_consume_returns_none_when_past_ttl(self):
        """A record older than ttl_seconds must not be consumed."""
        from src.monitor_prediction import register_tactical_prediction, consume_prediction
        open_predictions = {}
        pid = register_tactical_prediction(
            open_predictions, confidence=0.7, prediction_ttl_seconds=3600.0
        )
        # Force the record's age past TTL by rewriting created_at
        open_predictions[pid]["created_at"] -= 7200.0  # 2 hours old
        result = consume_prediction(open_predictions, pid, ttl_seconds=3600.0)
        assert result is None

    def test_consume_succeeds_when_within_ttl(self):
        """A fresh record must be consumable within TTL."""
        from src.monitor_prediction import register_tactical_prediction, consume_prediction
        open_predictions = {}
        pid = register_tactical_prediction(open_predictions, confidence=0.7)
        result = consume_prediction(open_predictions, pid, ttl_seconds=3600.0)
        assert result is not None
        assert result["confidence"] == 0.7

    def test_expired_record_is_not_consumed(self):
        """The expired record stays in the dict un-consumed so lookup_prediction
        can later distinguish "expired" from "missing" when computing prediction_binding.
        """
        from src.monitor_prediction import register_tactical_prediction, consume_prediction
        open_predictions = {}
        pid = register_tactical_prediction(open_predictions, confidence=0.7)
        open_predictions[pid]["created_at"] -= 7200.0
        consume_prediction(open_predictions, pid, ttl_seconds=3600.0)
        assert open_predictions[pid].get("consumed") is not True


# ============================================================================
# Task 1: prediction_binding echo on outcome_event
# ============================================================================

def _make_outcome_mock_db():
    """Shared mock DB for TestPredictionBindingEcho tests."""
    mock_db = MagicMock()
    mock_db.record_outcome_event = AsyncMock(return_value='oe-binding-1')
    mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
        'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
        'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
    })
    mock_db.get_latest_confidence_before = AsyncMock(return_value=None)
    return mock_db


class TestPredictionBindingEcho:
    """Six binding labels per spec §4. Each fallback path emits a distinct label.

    Tests use mock monitors with real _open_predictions dicts so the two-phase
    lookup (lookup_prediction → consume_prediction) runs against actual data
    structures rather than mocked method return values.
    """

    @pytest.mark.asyncio
    async def test_binding_registry_when_id_lives(self):
        """A fresh prediction_id resolves as 'registry'."""
        from src.monitor_prediction import register_tactical_prediction
        mock_db = _make_outcome_mock_db()

        open_predictions = {}
        pid = register_tactical_prediction(open_predictions, confidence=0.8)

        mock_monitor = MagicMock()
        mock_monitor._open_predictions = open_predictions
        mock_monitor._prediction_ttl_seconds = 3600.0
        mock_monitor._prev_confidence = None
        mock_monitor.get_primary_eisv.return_value = (0.7, 0.75, 0.15, -0.03)
        mock_monitor._behavioral_state = None

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-binding-reg'), \
             patch('src.mcp_handlers.context.get_context_client_session_id', return_value=None):

            mock_server.monitors = {'agent-binding-reg': mock_monitor}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                'prediction_id': pid,
            })

        parsed = parse_result(result)
        assert parsed.get('prediction_binding') == 'registry'
        _, db_kwargs = mock_db.record_outcome_event.call_args
        assert db_kwargs['detail']['prediction_binding'] == 'registry'
        # Record is consumed
        assert open_predictions[pid].get('consumed') is True

    @pytest.mark.asyncio
    async def test_binding_missing_prediction_when_id_unknown(self):
        """An id that doesn't exist in open_predictions resolves as 'missing_prediction'."""
        mock_db = _make_outcome_mock_db()

        open_predictions = {}  # empty — id will not be found

        mock_monitor = MagicMock()
        mock_monitor._open_predictions = open_predictions
        mock_monitor._prediction_ttl_seconds = 3600.0
        mock_monitor._prev_confidence = None
        mock_monitor._behavioral_state = None

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-binding-miss'), \
             patch('src.mcp_handlers.context.get_context_client_session_id', return_value=None):

            mock_server.monitors = {'agent-binding-miss': mock_monitor}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                'prediction_id': '00000000-0000-0000-0000-000000000000',
                'confidence': 0.5,
            })

        parsed = parse_result(result)
        assert parsed.get('prediction_binding') == 'missing_prediction'
        _, db_kwargs = mock_db.record_outcome_event.call_args
        assert db_kwargs['detail']['prediction_binding'] == 'missing_prediction'

    @pytest.mark.asyncio
    async def test_binding_ttl_expired_when_record_present_but_old(self):
        """A record that exists but is past TTL resolves as 'ttl_expired_fallback'."""
        from src.monitor_prediction import register_tactical_prediction
        mock_db = _make_outcome_mock_db()
        mock_db.get_latest_confidence_before = AsyncMock(return_value=None)

        open_predictions = {}
        pid = register_tactical_prediction(open_predictions, confidence=0.7)
        # Force record past TTL
        open_predictions[pid]['created_at'] -= 7200.0

        mock_monitor = MagicMock()
        mock_monitor._open_predictions = open_predictions
        mock_monitor._prediction_ttl_seconds = 3600.0
        mock_monitor._prev_confidence = None
        mock_monitor._behavioral_state = None

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-binding-ttl'), \
             patch('src.mcp_handlers.context.get_context_client_session_id', return_value=None):

            mock_server.monitors = {'agent-binding-ttl': mock_monitor}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                'prediction_id': pid,
                'confidence': 0.5,
            })

        parsed = parse_result(result)
        assert parsed.get('prediction_binding') == 'ttl_expired_fallback'
        _, db_kwargs = mock_db.record_outcome_event.call_args
        assert db_kwargs['detail']['prediction_binding'] == 'ttl_expired_fallback'
        # Record must NOT have been consumed
        assert open_predictions[pid].get('consumed') is not True

    @pytest.mark.asyncio
    async def test_binding_argument_fallback_when_no_id_supplied(self):
        """No prediction_id + confidence arg → 'argument_fallback'."""
        mock_db = _make_outcome_mock_db()

        mock_monitor = MagicMock()
        mock_monitor._open_predictions = {}
        mock_monitor._prediction_ttl_seconds = 3600.0
        mock_monitor._prev_confidence = None
        mock_monitor._behavioral_state = None

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-binding-arg'), \
             patch('src.mcp_handlers.context.get_context_client_session_id', return_value=None):

            mock_server.monitors = {'agent-binding-arg': mock_monitor}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                'confidence': 0.5,
                # No prediction_id
            })

        parsed = parse_result(result)
        assert parsed.get('prediction_binding') == 'argument_fallback'
        _, db_kwargs = mock_db.record_outcome_event.call_args
        assert db_kwargs['detail']['prediction_binding'] == 'argument_fallback'

    @pytest.mark.asyncio
    async def test_binding_no_binding_when_all_fallbacks_fail(self):
        """No id, no confidence arg, no monitor, no audit trail → 'no_binding'."""
        mock_db = _make_outcome_mock_db()
        mock_db.get_latest_confidence_before = AsyncMock(return_value=None)

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-binding-none'), \
             patch('src.mcp_handlers.context.get_context_client_session_id', return_value=None):

            mock_server.monitors = {}  # No monitor

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                # No prediction_id, no confidence
            })

        parsed = parse_result(result)
        assert parsed.get('prediction_binding') == 'no_binding'
        _, db_kwargs = mock_db.record_outcome_event.call_args
        assert db_kwargs['detail']['prediction_binding'] == 'no_binding'


# ============================================================================
# Task 1: Concurrency regression canary
# ============================================================================

class TestPredictionBindingConcurrencyCanary:
    """Regression canary, NOT a correctness assertion. Documents current
    behavior under racing outcome_events for the same prediction_id.
    The lock fix is explicitly deferred per spec §4. If this test ever
    starts failing because both calls resolve as `registry`, the race
    has become observable and the lock is no longer optional.
    """

    @pytest.mark.asyncio
    async def test_concurrent_outcome_events_one_wins_one_misses(self):
        """Under typical scheduling, one call consumes the prediction (registry)
        and the other misses (missing_prediction). Both must NOT resolve as registry
        simultaneously without a lock — which is the future failure mode this canary
        documents.
        """
        import asyncio
        from src.monitor_prediction import register_tactical_prediction
        mock_db = MagicMock()
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })
        mock_db.get_latest_confidence_before = AsyncMock(return_value=None)
        mock_db.record_outcome_event = AsyncMock(side_effect=['oe-c1', 'oe-c2'])

        open_predictions = {}
        pid = register_tactical_prediction(open_predictions, confidence=0.7)

        mock_monitor = MagicMock()
        mock_monitor._open_predictions = open_predictions
        mock_monitor._prediction_ttl_seconds = 3600.0
        mock_monitor._prev_confidence = None
        mock_monitor._behavioral_state = None

        from src.mcp_handlers.observability.outcome_events import handle_outcome_event

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-canary'), \
             patch('src.mcp_handlers.context.get_context_client_session_id', return_value=None):

            mock_server.monitors = {'agent-canary': mock_monitor}

            results = await asyncio.gather(
                handle_outcome_event({
                    'outcome_type': 'test_passed',
                    'prediction_id': pid,
                }),
                handle_outcome_event({
                    'outcome_type': 'test_passed',
                    'prediction_id': pid,
                }),
            )

        bindings = sorted(parse_result(r)['prediction_binding'] for r in results)
        # Under typical async scheduling: one wins (registry), one misses (missing_prediction).
        # Under unlucky scheduling without a lock, both could resolve as registry —
        # which is the failure mode this canary will eventually catch.
        assert bindings.count('registry') <= 1, (
            "Concurrency race made both calls resolve to registry — "
            "the lock fix deferred in v1 is no longer optional"
        )


# ============================================================================
# Task 3: verification_source round-trip
# ============================================================================

class TestVerificationSourceRoundTrip:
    """Confirm verification_source is passed to the mixin as a top-level column
    argument (not buried in detail JSONB) for both default and explicit values.
    Updated for migration 039: the field was promoted from detail JSONB to a
    top-level audit.outcome_events column. Uses the same mock-DB pattern as the
    rest of this module (no live DB required); inspects record_outcome_event
    call_args directly."""

    def _make_mock_db(self, outcome_id="oe-vs-1"):
        mock_db = MagicMock()
        mock_db.record_outcome_event = AsyncMock(return_value=outcome_id)
        mock_db.get_latest_eisv_by_agent_id = AsyncMock(return_value={
            'E': 0.7, 'I': 0.75, 'S': 0.15, 'V': -0.03,
            'phi': 0.1, 'verdict': 'safe', 'coherence': 0.48, 'regime': 'CONVERGENCE',
        })
        return mock_db

    @pytest.mark.asyncio
    async def test_default_recorded_as_column_arg(self):
        """Omitting verification_source defaults to 'agent_reported_tool_result' on the column arg.

        In production, params_step.py runs Pydantic validation before the handler,
        so the schema default is already present in `arguments` when the handler
        runs. This test mirrors that by including the default explicitly.
        Post-migration 039: the value is passed as a top-level column argument
        (kwargs['verification_source']), not embedded in detail JSONB.
        """
        mock_db = self._make_mock_db("oe-vs-default")

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-vs-1'), \
             patch('src.mcp_handlers.context.get_context_client_session_id', return_value='sess-vs-1'):

            mock_server.monitors = {}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            # Simulate post-Pydantic arguments dict: params_step fills schema defaults
            # before calling the handler, so verification_source is always present.
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                'confidence': 0.7,
                'verification_source': 'agent_reported_tool_result',
            })

        parsed = parse_result(result)
        assert parsed.get('outcome_id') == 'oe-vs-default'

        _, kwargs = mock_db.record_outcome_event.call_args
        assert kwargs.get('verification_source') == 'agent_reported_tool_result'

    @pytest.mark.asyncio
    async def test_server_observation_passed_as_column_arg(self):
        """Explicit verification_source='server_observation' is passed as column arg."""
        mock_db = self._make_mock_db("oe-vs-server")

        with patch('src.db.get_db', return_value=mock_db), \
             patch('src.mcp_handlers.observability.outcome_events.mcp_server') as mock_server, \
             patch('src.mcp_handlers.context.get_context_agent_id', return_value='agent-vs-2'), \
             patch('src.mcp_handlers.context.get_context_client_session_id', return_value='sess-vs-2'):

            mock_server.monitors = {}

            from src.mcp_handlers.observability.outcome_events import handle_outcome_event
            result = await handle_outcome_event({
                'outcome_type': 'test_passed',
                'confidence': 0.7,
                'verification_source': 'server_observation',
            })

        parsed = parse_result(result)
        assert parsed.get('outcome_id') == 'oe-vs-server'

        _, kwargs = mock_db.record_outcome_event.call_args
        assert kwargs.get('verification_source') == 'server_observation'
