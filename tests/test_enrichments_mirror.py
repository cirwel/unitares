"""
Tests for mirror signal enrichments in src/mcp_handlers/updates/enrichments.py.

Tests _detect_gaming, _generate_mirror_question,
and mirror KG-search gating helpers.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.updates.enrichments import (
    _detect_gaming,
    _generate_mirror_question,
    _should_search_kg_by_checkin_text,
)


# ============================================================================
# Helper: minimal UpdateContext mock
# ============================================================================

def _make_ctx(
    *,
    response_text="",
    task_type="mixed",
    complexity=0.5,
    confidence=None,
    verdict="proceed",
    arguments=None,
    response_data=None,
    total_updates=10,
    complexity_history=None,
    confidence_history=None,
):
    """Create a minimal UpdateContext-like object for testing."""
    ctx = MagicMock()
    ctx.response_text = response_text
    ctx.task_type = task_type
    ctx.complexity = complexity
    ctx.confidence = confidence
    ctx.arguments = arguments or {}
    ctx.response_data = response_data or {}
    ctx.agent_uuid = "test-uuid-1234"
    ctx.mcp_server = MagicMock()
    ctx.metrics_dict = {"verdict": verdict}
    ctx.meta = MagicMock()
    ctx.meta.total_updates = total_updates

    # Mock monitor with state histories
    monitor = MagicMock()
    state = MagicMock()

    if complexity_history is not None:
        state.complexity_history = complexity_history
    else:
        state.complexity_history = []

    if confidence_history is not None:
        state.confidence_history = confidence_history
    else:
        state.confidence_history = []

    monitor.state = state
    ctx.monitor = monitor

    return ctx


# ============================================================================
# _detect_gaming
# ============================================================================

class TestDetectGaming:

    def test_low_variance_complexity_detected(self):
        ctx = _make_ctx(complexity_history=[0.5, 0.5, 0.5, 0.5, 0.5])
        signals = _detect_gaming(ctx)
        assert len(signals) >= 1
        assert any("autopilot" in s.lower() for s in signals)

    def test_normal_variance_not_flagged(self):
        ctx = _make_ctx(complexity_history=[0.3, 0.5, 0.7, 0.4, 0.6])
        signals = _detect_gaming(ctx)
        # High variance should not trigger
        assert not any("autopilot" in s.lower() for s in signals)

    def test_too_few_reports_not_flagged(self):
        ctx = _make_ctx(complexity_history=[0.5, 0.5, 0.5])
        signals = _detect_gaming(ctx)
        assert len(signals) == 0

    def test_low_variance_confidence_detected(self):
        ctx = _make_ctx(
            complexity_history=[0.3, 0.5, 0.7, 0.4, 0.6],  # normal variance
            confidence_history=[0.8, 0.8, 0.8, 0.8, 0.8],  # low variance
        )
        signals = _detect_gaming(ctx)
        assert any("confidence" in s.lower() for s in signals)

    def test_no_monitor_returns_empty(self):
        ctx = _make_ctx()
        ctx.monitor = None
        signals = _detect_gaming(ctx)
        assert signals == []

    def test_near_identical_values_detected(self):
        ctx = _make_ctx(complexity_history=[0.501, 0.502, 0.500, 0.501, 0.500])
        signals = _detect_gaming(ctx)
        assert len(signals) >= 1
        assert any("variance" in s.lower() or "autopilot" in s.lower() for s in signals)


# ============================================================================
# _generate_mirror_question
# ============================================================================

class TestGenerateMirrorQuestion:

    def test_stuck_keyword_in_text_returns_unblock_question(self):
        ctx = _make_ctx(task_type="mixed", response_text="I'm stuck on this problem")
        question = _generate_mirror_question(ctx, signals=[])
        assert question is not None
        assert "unblock" in question.lower()

    def test_tight_margin_returns_edge_question(self):
        ctx = _make_ctx(
            response_data={"decision": {"margin": 0.05, "nearest_edge": "coherence"}}
        )
        question = _generate_mirror_question(ctx, signals=[])
        assert question is not None
        assert "coherence edge" in question.lower()

    def test_settling_margin_does_not_trigger_edge_question(self):
        ctx = _make_ctx(
            response_data={"decision": {"margin": "settling", "nearest_edge": None}}
        )
        question = _generate_mirror_question(ctx, signals=[])
        assert question is None

    def test_complexity_disagreement_no_longer_returns_question(self):
        # Complexity divergence is now surfaced as a neutral, recorded mirror
        # *signal* line (response_formatter._format_mirror), not as an
        # in-the-moment question demanding the agent justify itself on an
        # otherwise-healthy check-in. (2026-06-03)
        ctx = _make_ctx(
            response_data={
                "continuity": {
                    "self_reported_complexity": 0.8,
                    "derived_complexity": 0.22,
                    "complexity_divergence": 0.58,
                }
            }
        )
        question = _generate_mirror_question(ctx, signals=[])
        assert question is None

    def test_third_person_blocked_does_not_trigger_stuck_question(self):
        # Regression: a check-in that merely contains "blocked"/"stuck" while
        # describing a *resolved* or external event must not fabricate a
        # "you're stuck" nudge. Only genuine first-person stuckness should.
        for text in (
            "Cleared stale leases that blocked my edits; all green now.",
            "The fix unblocks future sessions.",
            "Investigated where the pipeline gets stuck for other agents.",
        ):
            ctx = _make_ctx(response_text=text)
            assert _generate_mirror_question(ctx, signals=[]) is None, text

    def test_autopilot_signal_returns_question(self):
        ctx = _make_ctx()
        question = _generate_mirror_question(ctx, signals=["Real work varies -- are you on autopilot?"])
        assert question is not None
        assert "actually changed" in question.lower()

    def test_steady_state_returns_none(self):
        ctx = _make_ctx(task_type="feature", response_text="Finished tracing the mirror output path.")
        question = _generate_mirror_question(ctx, signals=[])
        assert question is None


# ============================================================================
# KG search gating
# ============================================================================

class TestShouldSearchKgByCheckinText:

    def test_steady_state_skips_kg_search(self):
        ctx = _make_ctx(response_text="Completed a small cleanup without notable issues.")
        assert _should_search_kg_by_checkin_text(ctx, signals=[], question=None) is False

    def test_signal_or_question_enables_kg_search(self):
        ctx = _make_ctx(response_text="I am stuck investigating a regression in auth.")
        assert _should_search_kg_by_checkin_text(ctx, signals=[], question="What unblocks you?") is True
