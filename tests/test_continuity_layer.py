"""
Tests for dual-log continuity layer and operational/reflective log analysis.

Tests the pure functions in the dual-log architecture:
- analyze_response_text (text feature extraction)
- create_operational_entry (factory with session/latency tracking)
- OperationalEntry serialization roundtrip
- create_reflective_entry (factory with optional params)
- ReflectiveEntry serialization roundtrip
- derive_complexity (heuristic complexity from operational features)
- compute_continuity_metrics (grounding: operational vs reflective)
- ContinuityMetrics serialization
- ContinuityLayer (in-memory storage, no Redis)
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime, timedelta

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dual_log.operational import (
    OperationalEntry, analyze_response_text, create_operational_entry, KNOWN_TOOLS
)
from src.dual_log.reflective import (
    ReflectiveEntry, create_reflective_entry
)
from src.dual_log.continuity import (
    derive_complexity, tool_usage_complexity, compute_continuity_metrics,
    ContinuityMetrics, ContinuityLayer
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_op_entry(**overrides) -> OperationalEntry:
    """Build an OperationalEntry with sensible defaults, overridable."""
    defaults = dict(
        timestamp=datetime(2026, 2, 6, 12, 0, 0),
        agent_id="test-agent",
        response_tokens=0,
        response_chars=0,
        has_code_blocks=False,
        code_block_count=0,
        list_item_count=0,
        paragraph_count=1,
        question_count=0,
        latency_ms=None,
        client_session_id="sess-1",
        is_session_continuation=False,
        topic_hash="",
        mentioned_tools=[],
    )
    defaults.update(overrides)
    return OperationalEntry(**defaults)


def _make_refl_entry(**overrides) -> ReflectiveEntry:
    """Build a ReflectiveEntry with sensible defaults, overridable."""
    defaults = dict(
        timestamp=datetime(2026, 2, 6, 12, 0, 0),
        agent_id="test-agent",
        self_complexity=None,
        self_confidence=None,
        task_type=None,
        notes_count=0,
        insights_count=0,
        questions_count=0,
    )
    defaults.update(overrides)
    return ReflectiveEntry(**defaults)


# ===========================================================================
# 1. analyze_response_text
# ===========================================================================

class TestAnalyzeResponseText:
    """Tests for the pure text analysis function."""

    def test_empty_text_returns_all_zeros(self):
        result = analyze_response_text("")
        assert result["tokens"] == 0
        assert result["chars"] == 0
        assert result["has_code"] is False
        assert result["code_blocks"] == 0
        assert result["list_items"] == 0
        assert result["paragraphs"] == 0
        assert result["questions"] == 0
        assert result["topic_hash"] == ""
        assert result["tools"] == []

    def test_none_text_returns_all_zeros(self):
        result = analyze_response_text(None)
        assert result["tokens"] == 0
        assert result["tools"] == []

    def test_token_estimate_is_chars_div_four(self):
        text = "a" * 100
        result = analyze_response_text(text)
        assert result["tokens"] == 25
        assert result["chars"] == 100

    def test_code_blocks_detected(self):
        text = "Here is code:\n```python\nprint('hello')\n```\nAnd more:\n```\nfoo\n```"
        result = analyze_response_text(text)
        assert result["has_code"] is True
        assert result["code_blocks"] == 2

    def test_no_code_blocks(self):
        text = "Just plain text without any code."
        result = analyze_response_text(text)
        assert result["has_code"] is False
        assert result["code_blocks"] == 0

    def test_list_items_dash(self):
        text = "Items:\n- first\n- second\n- third"
        result = analyze_response_text(text)
        assert result["list_items"] == 3

    def test_list_items_asterisk(self):
        text = "Items:\n* alpha\n* beta"
        result = analyze_response_text(text)
        assert result["list_items"] == 2

    def test_list_items_numbered(self):
        text = "Steps:\n1. step one\n2. step two\n3. step three"
        result = analyze_response_text(text)
        assert result["list_items"] == 3

    def test_decimals_in_prose_are_not_list_items(self):
        """Regression: the ordered-list branch used to be unanchored, so any
        "<digits>. " sequence mid-sentence scored as a list item. A sensor
        bridge reporting period-separated decimals banked one phantom item per
        reading — Lumen's real status line scored 6 on a string with no list."""
        text = (
            "Anima state: calm. Warmth: 0.62. Clarity: 0.71. Stability: 0.55. "
            "Presence: 0.80. EISV: E=0.58, I=0.72, S=0.31, V=+0.14."
        )
        assert analyze_response_text(text)["list_items"] == 0

    def test_numbered_list_still_counts_when_line_anchored(self):
        """The fix must not cost real ordered lists — including indented ones."""
        text = "Plan:\n1. first\n  2. second indented\n3. third"
        assert analyze_response_text(text)["list_items"] == 3

    def test_paragraphs_single(self):
        text = "Just one paragraph of text."
        result = analyze_response_text(text)
        assert result["paragraphs"] == 1

    def test_paragraphs_multiple(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        result = analyze_response_text(text)
        assert result["paragraphs"] == 3

    def test_paragraphs_minimum_is_one(self):
        # Even a very short text gets paragraphs >= 1
        text = "Hello"
        result = analyze_response_text(text)
        assert result["paragraphs"] >= 1

    def test_questions_counted(self):
        text = "What is this? How does it work? Why?"
        result = analyze_response_text(text)
        assert result["questions"] == 3

    def test_no_questions(self):
        text = "This is a statement."
        result = analyze_response_text(text)
        assert result["questions"] == 0

    def test_topic_hash_is_deterministic(self):
        text = "Hello world"
        r1 = analyze_response_text(text)
        r2 = analyze_response_text(text)
        assert r1["topic_hash"] == r2["topic_hash"]
        assert len(r1["topic_hash"]) == 8

    def test_topic_hash_normalizes_whitespace_and_case(self):
        r1 = analyze_response_text("Hello   World")
        r2 = analyze_response_text("hello world")
        assert r1["topic_hash"] == r2["topic_hash"]

    def test_tool_mentions_detected(self):
        text = "I used process_agent_update and leave_note to track progress."
        result = analyze_response_text(text)
        assert "process_agent_update" in result["tools"]
        assert "leave_note" in result["tools"]

    def test_no_tool_mentions(self):
        text = "I just wrote some regular text."
        result = analyze_response_text(text)
        assert result["tools"] == []

    def test_tool_detection_is_case_insensitive(self):
        text = "Called PROCESS_AGENT_UPDATE successfully."
        result = analyze_response_text(text)
        assert "process_agent_update" in result["tools"]


# ===========================================================================
# 2. create_operational_entry
# ===========================================================================

class TestCreateOperationalEntry:
    """Tests for the operational entry factory function."""

    def test_basic_creation(self):
        entry = create_operational_entry(
            agent_id="agent-1",
            response_text="Hello world",
            client_session_id="sess-abc",
        )
        assert entry.agent_id == "agent-1"
        assert entry.client_session_id == "sess-abc"
        assert entry.response_chars == 11
        assert entry.response_tokens == 2  # 11 // 4
        assert entry.latency_ms is None
        assert entry.is_session_continuation is False

    def test_session_continuation_detected(self):
        entry = create_operational_entry(
            agent_id="agent-1",
            response_text="Hello",
            client_session_id="sess-X",
            prev_session_id="sess-X",
        )
        assert entry.is_session_continuation is True

    def test_session_not_continuation_when_different(self):
        entry = create_operational_entry(
            agent_id="agent-1",
            response_text="Hello",
            client_session_id="sess-X",
            prev_session_id="sess-Y",
        )
        assert entry.is_session_continuation is False

    def test_latency_calculated_from_prev_timestamp(self):
        prev = datetime.now() - timedelta(seconds=2)
        entry = create_operational_entry(
            agent_id="agent-1",
            response_text="Hello",
            client_session_id="sess-1",
            prev_timestamp=prev,
        )
        assert entry.latency_ms is not None
        # Should be approximately 2000ms (allow tolerance for execution time)
        assert 1800 < entry.latency_ms < 3000

    def test_no_latency_without_prev_timestamp(self):
        entry = create_operational_entry(
            agent_id="agent-1",
            response_text="Hello",
            client_session_id="sess-1",
        )
        assert entry.latency_ms is None


# ===========================================================================
# 3. OperationalEntry serialization roundtrip
# ===========================================================================

class TestOperationalEntrySerialization:
    """Tests for OperationalEntry.to_dict / from_dict roundtrip."""

    def test_roundtrip_preserves_all_fields(self):
        original = _make_op_entry(
            response_tokens=500,
            response_chars=2000,
            has_code_blocks=True,
            code_block_count=3,
            list_item_count=5,
            paragraph_count=4,
            question_count=2,
            latency_ms=1500.5,
            client_session_id="sess-rt",
            is_session_continuation=True,
            topic_hash="abcd1234",
            mentioned_tools=["leave_note", "onboard"],
        )
        data = original.to_dict()
        restored = OperationalEntry.from_dict(data)

        assert restored.agent_id == original.agent_id
        assert restored.response_tokens == original.response_tokens
        assert restored.response_chars == original.response_chars
        assert restored.has_code_blocks == original.has_code_blocks
        assert restored.code_block_count == original.code_block_count
        assert restored.list_item_count == original.list_item_count
        assert restored.paragraph_count == original.paragraph_count
        assert restored.question_count == original.question_count
        assert restored.latency_ms == original.latency_ms
        assert restored.client_session_id == original.client_session_id
        assert restored.is_session_continuation == original.is_session_continuation
        assert restored.topic_hash == original.topic_hash
        assert restored.mentioned_tools == original.mentioned_tools

    def test_timestamp_serializes_as_isoformat(self):
        entry = _make_op_entry()
        data = entry.to_dict()
        assert isinstance(data["timestamp"], str)
        # Should parse back without error
        datetime.fromisoformat(data["timestamp"])


# ===========================================================================
# 4. create_reflective_entry
# ===========================================================================

class TestCreateReflectiveEntry:
    """Tests for the reflective entry factory function."""

    def test_basic_creation_with_all_params(self):
        entry = create_reflective_entry(
            agent_id="agent-r",
            complexity=0.7,
            confidence=0.9,
            task_type="analysis",
            notes_count=3,
            insights_count=1,
            questions_count=2,
        )
        assert entry.agent_id == "agent-r"
        assert entry.self_complexity == 0.7
        assert entry.self_confidence == 0.9
        assert entry.task_type == "analysis"
        assert entry.notes_count == 3
        assert entry.insights_count == 1
        assert entry.questions_count == 2
        assert isinstance(entry.timestamp, datetime)

    def test_creation_with_minimal_params(self):
        entry = create_reflective_entry(agent_id="agent-m")
        assert entry.agent_id == "agent-m"
        assert entry.self_complexity is None
        assert entry.self_confidence is None
        assert entry.task_type is None
        assert entry.notes_count == 0


# ===========================================================================
# 5. ReflectiveEntry serialization roundtrip
# ===========================================================================

class TestReflectiveEntrySerialization:
    """Tests for ReflectiveEntry.to_dict / from_dict roundtrip."""

    def test_roundtrip_preserves_all_fields(self):
        original = _make_refl_entry(
            self_complexity=0.65,
            self_confidence=0.85,
            task_type="debugging",
            notes_count=4,
            insights_count=2,
            questions_count=1,
        )
        data = original.to_dict()
        restored = ReflectiveEntry.from_dict(data)

        assert restored.agent_id == original.agent_id
        assert restored.self_complexity == original.self_complexity
        assert restored.self_confidence == original.self_confidence
        assert restored.task_type == original.task_type
        assert restored.notes_count == original.notes_count
        assert restored.insights_count == original.insights_count
        assert restored.questions_count == original.questions_count

    def test_roundtrip_with_none_values(self):
        original = _make_refl_entry(self_complexity=None, self_confidence=None)
        data = original.to_dict()
        restored = ReflectiveEntry.from_dict(data)
        assert restored.self_complexity is None
        assert restored.self_confidence is None


# ===========================================================================
# 6. derive_complexity
# ===========================================================================

class TestDeriveComplexity:
    """Tests for the heuristic complexity derivation from operational features."""

    def test_zero_tokens_returns_zero(self):
        op = _make_op_entry(response_tokens=0)
        assert derive_complexity(op) == 0.0

    def test_high_tokens_approaches_one(self):
        op = _make_op_entry(response_tokens=5000)
        result = derive_complexity(op)
        # 0.45 * token_factor (capped at 1.0) = 0.45 at most from tokens alone
        assert result > 0.4

    def test_moderate_tokens_gives_moderate_complexity(self):
        op = _make_op_entry(response_tokens=500)
        result = derive_complexity(op)
        assert 0.1 < result < 0.5

    def test_code_blocks_increase_complexity(self):
        base = _make_op_entry(response_tokens=500)
        with_code = _make_op_entry(
            response_tokens=500,
            has_code_blocks=True,
            code_block_count=3,
        )
        assert derive_complexity(with_code) > derive_complexity(base)

    def test_list_items_increase_complexity(self):
        base = _make_op_entry(response_tokens=500)
        with_lists = _make_op_entry(response_tokens=500, list_item_count=10)
        assert derive_complexity(with_lists) > derive_complexity(base)

    def test_list_items_below_threshold_no_effect(self):
        # list_items <= 3 should not contribute to structure_factor
        base = _make_op_entry(response_tokens=500, list_item_count=0)
        with_few_lists = _make_op_entry(response_tokens=500, list_item_count=3)
        assert derive_complexity(with_few_lists) == derive_complexity(base)

    def test_paragraphs_increase_complexity(self):
        base = _make_op_entry(response_tokens=500, paragraph_count=1)
        with_paras = _make_op_entry(response_tokens=500, paragraph_count=7)
        assert derive_complexity(with_paras) > derive_complexity(base)

    def test_paragraphs_below_threshold_no_effect(self):
        # paragraph_count <= 2 should not contribute to structure_factor
        base = _make_op_entry(response_tokens=500, paragraph_count=1)
        with_two = _make_op_entry(response_tokens=500, paragraph_count=2)
        assert derive_complexity(with_two) == derive_complexity(base)

    def test_tool_mentions_increase_complexity(self):
        base = _make_op_entry(response_tokens=500)
        with_tools = _make_op_entry(
            response_tokens=500,
            mentioned_tools=["process_agent_update", "leave_note", "onboard"],
        )
        assert derive_complexity(with_tools) > derive_complexity(base)

    def test_questions_increase_complexity(self):
        base = _make_op_entry(response_tokens=500)
        with_questions = _make_op_entry(response_tokens=500, question_count=3)
        assert derive_complexity(with_questions) > derive_complexity(base)

    def test_result_bounded_zero_to_one(self):
        # Maximally complex entry
        op = _make_op_entry(
            response_tokens=10000,
            has_code_blocks=True,
            code_block_count=10,
            list_item_count=20,
            paragraph_count=15,
            question_count=10,
            mentioned_tools=["process_agent_update", "leave_note", "onboard", "identity"],
        )
        result = derive_complexity(op)
        assert 0.0 <= result <= 1.0

    def test_result_bounded_at_zero_for_empty(self):
        op = _make_op_entry(
            response_tokens=0,
            has_code_blocks=False,
            code_block_count=0,
            list_item_count=0,
            paragraph_count=0,
            question_count=0,
            mentioned_tools=[],
        )
        result = derive_complexity(op)
        assert result == 0.0


# ===========================================================================
# 7. compute_continuity_metrics
# ===========================================================================

class TestComputeContinuityMetrics:
    """Tests for the core grounding function that bridges operational and reflective."""

    def test_with_self_reported_complexity_computes_real_divergence(self):
        op = _make_op_entry(response_tokens=500)
        derived = derive_complexity(op)
        refl = _make_refl_entry(self_complexity=0.9)
        metrics = compute_continuity_metrics(op, refl)
        expected_divergence = abs(derived - 0.9)
        assert abs(metrics.complexity_divergence - expected_divergence) < 1e-6

    def test_without_self_reported_complexity_uses_default_divergence(self):
        op = _make_op_entry(response_tokens=500)
        refl = _make_refl_entry(self_complexity=None)
        metrics = compute_continuity_metrics(op, refl)
        assert metrics.complexity_divergence == 0.2

    def test_overconfidence_signal(self):
        # High confidence (>0.8) + high derived complexity (>0.6) => overconfidence
        op = _make_op_entry(
            response_tokens=5000,
            has_code_blocks=True,
            code_block_count=5,
            list_item_count=15,
            paragraph_count=8,
            question_count=3,
            mentioned_tools=["process_agent_update", "leave_note", "onboard", "identity"],
        )
        # Verify derived complexity is high enough
        derived = derive_complexity(op)
        assert derived > 0.6, f"Need derived > 0.6 for test, got {derived}"
        refl = _make_refl_entry(self_confidence=0.85)
        metrics = compute_continuity_metrics(op, refl)
        assert metrics.overconfidence_signal is True

    def test_no_overconfidence_when_low_complexity(self):
        op = _make_op_entry(response_tokens=50)
        refl = _make_refl_entry(self_confidence=0.9)
        metrics = compute_continuity_metrics(op, refl)
        assert metrics.overconfidence_signal is False

    def test_underconfidence_signal(self):
        # Low confidence (<0.3) + low derived complexity (<0.3)
        op = _make_op_entry(response_tokens=100)
        derived = derive_complexity(op)
        assert derived < 0.3, f"Need derived < 0.3 for test, got {derived}"
        refl = _make_refl_entry(self_confidence=0.2)
        metrics = compute_continuity_metrics(op, refl)
        assert metrics.underconfidence_signal is True

    def test_no_underconfidence_when_high_complexity(self):
        op = _make_op_entry(response_tokens=3000)
        refl = _make_refl_entry(self_confidence=0.2)
        metrics = compute_continuity_metrics(op, refl)
        assert metrics.underconfidence_signal is False

    def test_no_confidence_signals_without_self_confidence(self):
        op = _make_op_entry(response_tokens=500)
        refl = _make_refl_entry(self_confidence=None)
        metrics = compute_continuity_metrics(op, refl)
        assert metrics.overconfidence_signal is False
        assert metrics.underconfidence_signal is False

    def test_e_input_from_latency(self):
        op = _make_op_entry(response_tokens=400, latency_ms=2000.0)
        refl = _make_refl_entry()
        metrics = compute_continuity_metrics(op, refl)
        # tokens_per_sec = 400 / (2000 / 1000) = 200
        # E_input = clip(200 / 200, 0.3, 1.0) = 1.0
        assert abs(metrics.E_input - 1.0) < 1e-6

    def test_e_input_without_latency(self):
        op = _make_op_entry(response_tokens=500, latency_ms=None)
        refl = _make_refl_entry()
        metrics = compute_continuity_metrics(op, refl)
        # E_input = clip(0.5 + 0.3 * (500 / 1000), 0.3, 1.0) = clip(0.65, 0.3, 1.0) = 0.65
        assert abs(metrics.E_input - 0.65) < 1e-6

    def test_e_input_clamped_low(self):
        # Very slow: very few tokens over a long period
        op = _make_op_entry(response_tokens=10, latency_ms=10000.0)
        refl = _make_refl_entry()
        metrics = compute_continuity_metrics(op, refl)
        # tokens_per_sec = 10 / 10 = 1.0
        # E_input = clip(1 / 200, 0.3, 1.0) = 0.3
        assert abs(metrics.E_input - 0.3) < 1e-6

    def test_i_input_equals_one_minus_divergence(self):
        op = _make_op_entry(response_tokens=500)
        refl = _make_refl_entry(self_complexity=0.5)
        metrics = compute_continuity_metrics(op, refl)
        expected_i = 1.0 - metrics.complexity_divergence
        assert abs(metrics.I_input - expected_i) < 1e-6

    def test_s_input_combines_multiple_sources(self):
        # Not a session continuation + no self_complexity => extra uncertainty
        op = _make_op_entry(
            response_tokens=500,
            is_session_continuation=False,
        )
        refl = _make_refl_entry(self_complexity=None)
        metrics = compute_continuity_metrics(op, refl)
        # S_input = clip(0.1 + 0.5*0.2 + 0.1 + 0.1, 0, 1) = clip(0.4, 0, 1) = 0.4
        assert abs(metrics.S_input - 0.4) < 1e-6

    def test_s_input_lower_with_session_continuation_and_self_report(self):
        op = _make_op_entry(
            response_tokens=500,
            is_session_continuation=True,
        )
        derived = derive_complexity(op)
        refl = _make_refl_entry(self_complexity=derived)  # perfect match => divergence 0
        metrics = compute_continuity_metrics(op, refl)
        # S_input = clip(0.1 + 0.5*0.0 + 0 + 0, 0, 1) = 0.1
        assert abs(metrics.S_input - 0.1) < 1e-6

    def test_calibration_weight_passed_through(self):
        op = _make_op_entry(response_tokens=500)
        refl = _make_refl_entry()
        metrics = compute_continuity_metrics(op, refl, calibration_weight=0.75)
        assert metrics.calibration_weight == 0.75


# ===========================================================================
# 7b. Substrate-portability canaries (observe-only)
# ===========================================================================

def _lumen_status(mood="calm", w=0.62, c=0.81, s=0.77, p=0.80,
                  E=0.58, I=0.82, S=0.21, V=0.34, marks=47):
    """Reproduces anima_mcp.eisv_mapper.generate_status_text's real shape:
    a single-line, period-joined template. This is what an embodied agent
    actually puts in response_text."""
    return (
        f"Anima state: {mood}. Warmth: {w:.2f}. Clarity: {c:.2f}. "
        f"Stability: {s:.2f}. Presence: {p:.2f}. "
        f"EISV: E={E:.2f}, I={I:.2f}, S={S:.2f}, V={V:+.2f}. "
        f"Experience: {marks} marks."
    )


class TestSubstrateCanaries:
    """The canaries record whether a continuity feature was a measurement or a
    default. They must never change the math — only describe it."""

    def test_self_complexity_defaulted_flags_the_stand_in(self):
        op = create_operational_entry("a", "Some prose response.", "s1")
        without = compute_continuity_metrics(op, create_reflective_entry("a"))
        assert without.self_complexity_defaulted is True
        assert without.complexity_divergence == 0.2  # unchanged stand-in

        with_report = compute_continuity_metrics(
            op, create_reflective_entry("a", complexity=0.6)
        )
        assert with_report.self_complexity_defaulted is False

    def test_e_input_clipped_flags_the_floor(self):
        """E_input's /200 normalizer is fed the inter-check-in gap, not
        generation time, so it pins to the 0.3 floor for realistic traffic.
        The flag makes that visible instead of looking like a measurement."""
        op = create_operational_entry(
            "a", "word " * 500, "s1",
            prev_session_id="s1",
            prev_timestamp=datetime.now() - timedelta(seconds=60),
        )
        m = compute_continuity_metrics(op, create_reflective_entry("a"))
        assert m.E_input == pytest.approx(0.3)
        assert m.E_input_clipped is True

    def test_templated_status_line_is_flagged_degenerate(self):
        op = create_operational_entry("lumen", _lumen_status(), "s1")
        m = compute_continuity_metrics(op, create_reflective_entry("lumen"))
        assert m.continuity_degenerate is True

    def test_markdown_assistant_turn_is_not_degenerate(self):
        text = (
            "I traced the failure to the retry loop.\n\n"
            "- the except clause is too broad\n- the counter resets\n"
            "- no backoff\n- the log line is debug-level\n\n"
            "Want me to narrow the except first?"
        )
        op = create_operational_entry("claude", text, "s1")
        m = compute_continuity_metrics(op, create_reflective_entry("claude"))
        assert m.continuity_degenerate is False

    def test_derived_complexity_is_inert_across_embodied_state(self):
        """The finding the canaries exist to surface: sweeping an embodied
        agent from healthy to near-dead barely moves the operational reading,
        because the template's structure does not depend on the state it
        reports. Guards against a future 'grounding' claim over this channel
        for non-linguistic substrates."""
        healthy = _lumen_status("calm", 0.62, 0.81, 0.77, 0.80,
                                0.58, 0.82, 0.21, 0.34, 47)
        dying = _lumen_status("flat", 0.02, 0.05, 0.03, 0.01,
                              0.03, 0.09, 0.07, -0.02, 0)
        spread = abs(
            derive_complexity(create_operational_entry("l", healthy, "s"))
            - derive_complexity(create_operational_entry("l", dying, "s"))
        )
        assert spread < 0.01, (
            f"operational channel moved {spread:.4f} between a healthy and a "
            "near-dead embodied agent; if this ever becomes informative, "
            "revisit continuity_degenerate rather than deleting the test"
        )


# ===========================================================================
# 8. ContinuityMetrics serialization
# ===========================================================================

class TestContinuityMetricsSerialization:
    """Tests for ContinuityMetrics.to_dict()."""

    def test_to_dict_contains_all_fields(self):
        metrics = ContinuityMetrics(
            timestamp=datetime(2026, 2, 6, 12, 0, 0),
            agent_id="test-agent",
            derived_complexity=0.45,
            self_complexity=0.5,
            complexity_divergence=0.05,
            overconfidence_signal=False,
            underconfidence_signal=False,
            E_input=0.7,
            I_input=0.95,
            S_input=0.15,
            calibration_weight=0.6,
        )
        d = metrics.to_dict()
        assert d["agent_id"] == "test-agent"
        assert d["derived_complexity"] == 0.45
        assert d["self_complexity"] == 0.5
        assert d["complexity_divergence"] == 0.05
        assert d["overconfidence_signal"] is False
        assert d["underconfidence_signal"] is False
        assert d["E_input"] == 0.7
        assert d["I_input"] == 0.95
        assert d["S_input"] == 0.15
        assert d["calibration_weight"] == 0.6
        assert isinstance(d["timestamp"], str)

    def test_to_dict_with_none_self_complexity(self):
        metrics = ContinuityMetrics(
            timestamp=datetime(2026, 2, 6, 12, 0, 0),
            agent_id="test-agent",
            derived_complexity=0.3,
            self_complexity=None,
            complexity_divergence=0.2,
            overconfidence_signal=False,
            underconfidence_signal=False,
            E_input=0.5,
            I_input=0.8,
            S_input=0.3,
        )
        d = metrics.to_dict()
        assert d["self_complexity"] is None


# ===========================================================================
# 9. ContinuityLayer (in-memory, no Redis)
# ===========================================================================

class TestContinuityLayerInMemory:
    """Tests for the ContinuityLayer using in-memory storage (no Redis)."""

    def test_process_update_returns_continuity_metrics(self):
        layer = ContinuityLayer(agent_id="layer-agent", redis_client=None)
        metrics = layer.process_update(
            response_text="This is a test response.",
            self_complexity=0.5,
            self_confidence=0.7,
            client_session_id="sess-layer",
        )
        assert isinstance(metrics, ContinuityMetrics)
        assert metrics.agent_id == "layer-agent"
        assert metrics.self_complexity == 0.5

    def test_get_recent_metrics_returns_stored_entries(self):
        layer = ContinuityLayer(agent_id="metrics-agent", redis_client=None)
        layer.process_update(
            response_text="First response.",
            client_session_id="sess-1",
        )
        layer.process_update(
            response_text="Second response with more content.",
            client_session_id="sess-1",
        )
        recent = layer.get_recent_metrics(count=10)
        assert len(recent) == 2
        # Most recent first (reversed)
        assert isinstance(recent[0], dict)

    def test_get_recent_metrics_empty_initially(self):
        layer = ContinuityLayer(agent_id="empty-agent", redis_client=None)
        assert layer.get_recent_metrics() == []

    def test_get_cumulative_divergence_sums_divergences(self):
        layer = ContinuityLayer(agent_id="div-agent", redis_client=None)
        layer.process_update(
            response_text="Short.",
            self_complexity=0.9,
            client_session_id="sess-1",
        )
        layer.process_update(
            response_text="Another short reply.",
            self_complexity=0.8,
            client_session_id="sess-1",
        )
        cumulative = layer.get_cumulative_divergence(window_count=10)
        assert cumulative > 0.0

    def test_get_cumulative_divergence_zero_when_empty(self):
        layer = ContinuityLayer(agent_id="empty-div", redis_client=None)
        assert layer.get_cumulative_divergence() == 0.0

    def test_session_continuation_tracked_across_updates(self):
        layer = ContinuityLayer(agent_id="cont-agent", redis_client=None)
        m1 = layer.process_update(
            response_text="First message.",
            client_session_id="sess-A",
        )
        # Second update with same session
        m2 = layer.process_update(
            response_text="Second message in same session.",
            client_session_id="sess-A",
        )
        # The internal state should have tracked prev_session_id
        assert layer._prev_session_id == "sess-A"

    def test_latency_tracked_across_updates(self):
        layer = ContinuityLayer(agent_id="lat-agent", redis_client=None)
        layer.process_update(
            response_text="First.",
            client_session_id="sess-1",
        )
        # After first update, _prev_timestamp should be set
        assert layer._prev_timestamp is not None

    def test_max_log_entries_enforced(self):
        layer = ContinuityLayer(agent_id="trim-agent", redis_client=None)
        # Process more than MAX_LOG_ENTRIES updates
        for i in range(ContinuityLayer.MAX_LOG_ENTRIES + 10):
            layer.process_update(
                response_text=f"Response number {i}.",
                client_session_id="sess-1",
            )
        key = f"cont:trim-agent"
        stored = layer._memory_storage.get(key, [])
        assert len(stored) <= ContinuityLayer.MAX_LOG_ENTRIES


# ---------------------------------------------------------------------------
# tool_usage_complexity
# ---------------------------------------------------------------------------

class TestToolUsageComplexity:
    """Tests for tool_usage_complexity()."""

    def test_zero_calls_returns_baseline(self):
        assert tool_usage_complexity(total_calls=0) == 0.2

    def test_simple_usage_low_complexity(self):
        """One tool, few calls, no errors, one file → low complexity."""
        c = tool_usage_complexity(
            unique_tools=1, total_calls=3, error_rate=0.0, files_modified=1
        )
        assert c < 0.35

    def test_complex_usage_high_complexity(self):
        """Many tools, many calls, some errors, many files → high complexity."""
        c = tool_usage_complexity(
            unique_tools=6, total_calls=40, error_rate=0.3, files_modified=10
        )
        assert c > 0.6

    def test_more_tools_increases_complexity(self):
        base = dict(total_calls=10, error_rate=0.1, files_modified=2)
        c_few = tool_usage_complexity(unique_tools=1, **base)
        c_many = tool_usage_complexity(unique_tools=5, **base)
        assert c_many > c_few

    def test_more_files_increases_complexity(self):
        base = dict(unique_tools=2, total_calls=10, error_rate=0.0)
        c_one = tool_usage_complexity(files_modified=1, **base)
        c_many = tool_usage_complexity(files_modified=8, **base)
        assert c_many > c_one

    def test_higher_error_rate_increases_complexity(self):
        base = dict(unique_tools=2, total_calls=10, files_modified=2)
        c_clean = tool_usage_complexity(error_rate=0.0, **base)
        c_errors = tool_usage_complexity(error_rate=0.5, **base)
        assert c_errors > c_clean

    def test_bounded_zero_to_one(self):
        """Output always in [0, 1] even with extreme inputs."""
        assert 0.0 <= tool_usage_complexity(
            unique_tools=100, total_calls=10000, error_rate=1.0, files_modified=100
        ) <= 1.0
        assert 0.0 <= tool_usage_complexity(
            unique_tools=0, total_calls=0, error_rate=0.0, files_modified=0
        ) <= 1.0


# ---------------------------------------------------------------------------
# ContinuityLayer with tool_usage_stats
# ---------------------------------------------------------------------------

class TestContinuityLayerToolUsage:
    """Tests for tool_usage_stats integration in ContinuityLayer."""

    def test_no_tool_stats_same_as_before(self):
        """Passing tool_usage_stats=None doesn't change behavior."""
        layer = ContinuityLayer(agent_id="test-no-tool", redis_client=None)
        m1 = layer.process_update(
            response_text="Fixed the bug in auth module.",
            self_complexity=0.5,
            client_session_id="s1",
        )
        layer2 = ContinuityLayer(agent_id="test-no-tool2", redis_client=None)
        m2 = layer2.process_update(
            response_text="Fixed the bug in auth module.",
            self_complexity=0.5,
            client_session_id="s1",
            tool_usage_stats=None,
        )
        assert abs(m1.derived_complexity - m2.derived_complexity) < 0.01

    def test_tool_stats_blend_into_derived_complexity(self):
        """Tool usage stats should shift derived_complexity."""
        layer = ContinuityLayer(agent_id="test-blend", redis_client=None)
        # Simple text, but complex tool usage
        m = layer.process_update(
            response_text="Done.",
            client_session_id="s1",
            tool_usage_stats={
                "unique_tools": 6,
                "total_calls": 40,
                "error_rate": 0.2,
                "files_modified": 10,
            },
        )
        layer2 = ContinuityLayer(agent_id="test-no-blend", redis_client=None)
        m2 = layer2.process_update(
            response_text="Done.",
            client_session_id="s1",
        )
        # "Done." has very low text complexity, so tool usage should raise it
        assert m.derived_complexity > m2.derived_complexity

    def test_cross_validation_strengthens_divergence(self):
        """When both text and tool derivation disagree with self-report, divergence increases."""
        layer = ContinuityLayer(agent_id="test-cross", redis_client=None)
        # Agent claims low complexity but tool usage shows high complexity
        m = layer.process_update(
            response_text="Simple one-liner fix.",
            self_complexity=0.1,
            client_session_id="s1",
            tool_usage_stats={
                "unique_tools": 5,
                "total_calls": 30,
                "error_rate": 0.3,
                "files_modified": 8,
            },
        )
        layer2 = ContinuityLayer(agent_id="test-no-cross", redis_client=None)
        m2 = layer2.process_update(
            response_text="Simple one-liner fix.",
            self_complexity=0.1,
            client_session_id="s1",
        )
        # Cross-validation should strengthen divergence when both signals disagree
        assert m.complexity_divergence >= m2.complexity_divergence
