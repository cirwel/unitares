"""
Tests for mirror signal enrichments in src/mcp_handlers/updates/enrichments.py.

Tests _detect_gaming, _generate_mirror_reflection,
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
    _generate_mirror_reflection,
    _should_search_kg_by_checkin_text,
    _proactive_kg_due,
    _dedupe_surfaced_kg,
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

    def test_records_capture_structured_trigger(self):
        # Phase 0 instrumentation: structured trigger record alongside prose.
        records = []
        ctx = _make_ctx(
            complexity_history=[0.5, 0.5, 0.5, 0.5, 0.5],
            confidence_history=[0.8, 0.8, 0.8, 0.8, 0.8],
        )
        signals = _detect_gaming(ctx, records=records)
        assert len(signals) >= 1
        assert len(records) >= 1
        types = {r["signal_type"] for r in records}
        assert "autopilot_complexity" in types
        assert "autopilot_confidence" in types
        for r in records:
            assert r["metric"].endswith("_variance")
            assert r["threshold"] == 0.005
            assert isinstance(r["value"], float)

    def test_fired_records_marked_fired(self):
        records = []
        _detect_gaming(_make_ctx(complexity_history=[0.5, 0.5, 0.5, 0.5, 0.5]), records=records)
        assert records and all(r["fired"] is True for r in records)

    def test_near_threshold_nonfired_records_logged(self):
        # Phase 0.5: variance in [0.005, 0.010) -> NO prose, but a fired=False
        # control record for the RDD. Histories below give variance ~0.008.
        records = []
        ctx = _make_ctx(
            complexity_history=[0.5, 0.5, 0.5, 0.5, 0.7],
            confidence_history=[0.8, 0.8, 0.8, 0.8, 1.0],
        )
        signals = _detect_gaming(ctx, records=records)
        assert signals == []  # near-band fires no agent-facing line
        assert len(records) == 2
        for r in records:
            assert r["fired"] is False
            assert 0.005 <= r["value"] < 0.010

    def test_high_variance_logs_nothing(self):
        # Above the near band: no prose and no record at all.
        records = []
        signals = _detect_gaming(
            _make_ctx(complexity_history=[0.3, 0.5, 0.7, 0.4, 0.9]), records=records)
        assert signals == []
        assert records == []

    def test_records_optional_back_compat(self):
        # Called without records (the historical signature) still returns prose.
        ctx = _make_ctx(complexity_history=[0.5, 0.5, 0.5, 0.5, 0.5])
        signals = _detect_gaming(ctx)
        assert any("autopilot" in s.lower() for s in signals)

    def test_signals_are_observational_not_interrogative(self):
        # #583 discipline ("reflect, don't advise") applies to the raw gaming
        # signals too, not just the distilled reflection: a mirror line states
        # the fact, it does not interrogate or prescribe.
        ctx = _make_ctx(
            complexity_history=[0.5, 0.5, 0.5, 0.5, 0.5],
            confidence_history=[0.8, 0.8, 0.8, 0.8, 0.8],
        )
        signals = _detect_gaming(ctx)
        assert len(signals) >= 1
        for s in signals:
            assert "?" not in s, f"gaming signal is interrogative: {s!r}"
            assert "consider " not in s.lower(), f"gaming signal prescribes: {s!r}"


# ============================================================================
# _generate_mirror_reflection
# ============================================================================

class TestGenerateMirrorReflection:

    def test_stuck_keyword_in_text_reflects_stuck(self):
        ctx = _make_ctx(task_type="mixed", response_text="I'm stuck on this problem")
        reflection = _generate_mirror_reflection(ctx, signals=[])
        assert reflection is not None
        # Descriptive reflection of their own words — NOT a directive ("what would unblock you?").
        assert "stuck" in reflection.lower()
        assert "?" not in reflection

    def test_tight_margin_returns_edge_question(self):
        ctx = _make_ctx(
            response_data={"decision": {"margin": 0.05, "nearest_edge": "coherence"}}
        )
        question = _generate_mirror_reflection(ctx, signals=[])
        assert question is not None
        assert "coherence edge" in question.lower()

    def test_settling_margin_does_not_trigger_edge_question(self):
        ctx = _make_ctx(
            response_data={"decision": {"margin": "settling", "nearest_edge": None}}
        )
        question = _generate_mirror_reflection(ctx, signals=[])
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
        question = _generate_mirror_reflection(ctx, signals=[])
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
            assert _generate_mirror_reflection(ctx, signals=[]) is None, text

    def test_autopilot_signal_reflects_repetition(self):
        ctx = _make_ctx()
        reflection = _generate_mirror_reflection(
            ctx, signals=["Your last 5 complexity reports were all 0.50 — no variance, reads as autopilot."]
        )
        assert reflection is not None
        assert "repetitive" in reflection.lower()
        assert "?" not in reflection

    def test_steady_state_returns_none(self):
        ctx = _make_ctx(task_type="feature", response_text="Finished tracing the mirror output path.")
        question = _generate_mirror_reflection(ctx, signals=[])
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

    def test_stale_complexity_gap_skips_kg_search(self):
        """Council fold (PR #603): a stable session-long complexity gap is
        novelty-gated out of the mirror line — the KG search it used to
        trigger on every check-in must respect the same gate."""
        ctx = _make_ctx(
            response_text="Continuing the same long-running refactor as before.",
            response_data={
                "continuity": {
                    "self_reported_complexity": 0.8,
                    "derived_complexity": 0.22,
                    "complexity_divergence": 0.58,
                    "divergence_novel": False,
                }
            },
        )
        assert _should_search_kg_by_checkin_text(ctx, signals=[], question=None) is False

    def test_novel_complexity_gap_still_enables_kg_search(self):
        ctx = _make_ctx(
            response_text="Continuing the same long-running refactor as before.",
            response_data={
                "continuity": {
                    "self_reported_complexity": 0.8,
                    "derived_complexity": 0.22,
                    "complexity_divergence": 0.58,
                    "divergence_novel": True,
                }
            },
        )
        assert _should_search_kg_by_checkin_text(ctx, signals=[], question=None) is True

    def test_legacy_payload_without_novelty_key_still_enables_kg_search(self):
        ctx = _make_ctx(
            response_text="Continuing the same long-running refactor as before.",
            response_data={
                "continuity": {
                    "self_reported_complexity": 0.8,
                    "derived_complexity": 0.22,
                    "complexity_divergence": 0.58,
                }
            },
        )
        assert _should_search_kg_by_checkin_text(ctx, signals=[], question=None) is True


# ============================================================================
# Proactive KG surfacing gate (adoption v0)
# ============================================================================

LONG_TEXT = "Implementing the new retrieval gate on the check-in response path."


class TestProactiveKgDue:

    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("UNITARES_KG_PROACTIVE_EVERY", raising=False)
        ctx = _make_ctx(response_text=LONG_TEXT, total_updates=10)
        assert _proactive_kg_due(ctx) is False

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("UNITARES_KG_PROACTIVE_EVERY", "0")
        ctx = _make_ctx(response_text=LONG_TEXT, total_updates=10)
        assert _proactive_kg_due(ctx) is False

    def test_fires_on_cadence_multiple(self, monkeypatch):
        monkeypatch.setenv("UNITARES_KG_PROACTIVE_EVERY", "5")
        ctx = _make_ctx(response_text=LONG_TEXT, total_updates=10)
        assert _proactive_kg_due(ctx) is True

    def test_skips_off_cadence(self, monkeypatch):
        monkeypatch.setenv("UNITARES_KG_PROACTIVE_EVERY", "5")
        ctx = _make_ctx(response_text=LONG_TEXT, total_updates=12)
        assert _proactive_kg_due(ctx) is False

    def test_skips_during_warmup(self, monkeypatch):
        # total <= 3 is settling; even an on-cadence value must not fire.
        monkeypatch.setenv("UNITARES_KG_PROACTIVE_EVERY", "3")
        ctx = _make_ctx(response_text=LONG_TEXT, total_updates=3)
        assert _proactive_kg_due(ctx) is False

    def test_skips_terse_text(self, monkeypatch):
        monkeypatch.setenv("UNITARES_KG_PROACTIVE_EVERY", "5")
        ctx = _make_ctx(response_text="done", total_updates=10)
        assert _proactive_kg_due(ctx) is False

    def test_malformed_env_is_safe(self, monkeypatch):
        monkeypatch.setenv("UNITARES_KG_PROACTIVE_EVERY", "not-an-int")
        ctx = _make_ctx(response_text=LONG_TEXT, total_updates=10)
        assert _proactive_kg_due(ctx) is False


# ============================================================================
# Proactive KG surfacing — session dedup (novelty gate)
# ============================================================================

def _fake_redis(sadd_returns):
    """A redis whose sadd returns values popped from a list (1=new, 0=seen)."""
    redis = MagicMock()
    seq = list(sadd_returns)

    async def _sadd(key, member):
        return seq.pop(0) if seq else 0

    async def _expire(key, ttl):
        return True

    redis.sadd = _sadd
    redis.expire = _expire
    return redis


class TestDedupeSurfacedKg:

    @pytest.mark.asyncio
    async def test_empty_passthrough(self):
        ctx = _make_ctx()
        assert await _dedupe_surfaced_kg(ctx, []) == []

    @pytest.mark.asyncio
    async def test_no_redis_fails_open(self):
        ctx = _make_ctx()
        results = [{"discovery_id": "d1", "summary": "x"}]
        with patch("src.cache.redis_client.get_redis", AsyncMock(return_value=None)):
            out = await _dedupe_surfaced_kg(ctx, results)
        assert out == results  # fail-open: keep the nudge

    @pytest.mark.asyncio
    async def test_drops_already_seen(self):
        ctx = _make_ctx()
        results = [
            {"discovery_id": "d1", "summary": "new"},
            {"discovery_id": "d2", "summary": "seen"},
        ]
        # d1 newly added (1), d2 already in set (0) → only d1 survives.
        redis = _fake_redis([1, 0])
        with patch("src.cache.redis_client.get_redis", AsyncMock(return_value=redis)):
            out = await _dedupe_surfaced_kg(ctx, results)
        assert [r["discovery_id"] for r in out] == ["d1"]

    @pytest.mark.asyncio
    async def test_entry_without_id_is_kept(self):
        ctx = _make_ctx()
        results = [{"summary": "no id here"}]
        redis = _fake_redis([])
        with patch("src.cache.redis_client.get_redis", AsyncMock(return_value=redis)):
            out = await _dedupe_surfaced_kg(ctx, results)
        assert out == results
