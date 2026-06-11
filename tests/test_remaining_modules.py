"""
Tests for modules with low coverage:
  - llm_delegation, model_inference, dialectic_reviewer,
    dialectic_calibration, dialectic_resolution, condition_parser,
    utils, middleware, audit_log
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# 1. condition_parser  (68 % -> higher)
# ---------------------------------------------------------------------------
from src.mcp_handlers.support.condition_parser import (
    ParsedCondition,
    parse_condition,
    _normalize_target,
    apply_condition,
)


class TestParsedCondition:
    def test_to_dict(self):
        pc = ParsedCondition(action="set", target="complexity", value=0.3, unit=None)
        pc.original = "Set complexity to 0.3"
        d = pc.to_dict()
        assert d["action"] == "set"
        assert d["target"] == "complexity"
        assert d["value"] == 0.3
        assert d["unit"] is None
        assert d["original"] == "Set complexity to 0.3"

    def test_defaults(self):
        pc = ParsedCondition(action="unknown", target="unknown")
        assert pc.value is None
        assert pc.unit is None
        assert pc.original == ""


class TestParseCondition:
    # Pattern 1 – Reduce/Increase/Set X to Y
    @pytest.mark.parametrize("text,action,target,value", [
        ("Reduce complexity to 0.3", "reduce", "complexity", 0.3),
        ("Lower risk to 0.5", "reduce", "risk_score", 0.5),
        ("Increase coherence to 0.8", "increase", "coherence", 0.8),
        ("Raise coherence to 0.9", "increase", "coherence", 0.9),
        ("Set complexity to 0.4", "set", "complexity", 0.4),
    ])
    def test_pattern1(self, text, action, target, value):
        p = parse_condition(text)
        assert p.action == action
        assert p.target == target
        assert p.value == pytest.approx(value)

    # Pattern 2 – Monitor for X hours/minutes
    @pytest.mark.parametrize("text,value,unit", [
        ("Monitor for 24 hours", 24.0, "hours"),
        ("Monitor for 30 minutes", 30.0, "minutes"),
        ("Monitor for 2h", 2.0, "hours"),
        ("Monitor for 5m", 5.0, "minutes"),
    ])
    def test_pattern2(self, text, value, unit):
        p = parse_condition(text)
        assert p.action == "monitor"
        assert p.target == "monitoring_duration"
        assert p.value == pytest.approx(value)
        assert p.unit == unit

    # Pattern 3 – Keep X below/above Y
    @pytest.mark.parametrize("text,direction", [
        ("Keep risk below 0.5", "below"),
        ("Keep risk above 0.2", "above"),
        ("Keep coherence under 0.9", "below"),
        ("Keep coherence over 0.3", "above"),
    ])
    def test_pattern3(self, text, direction):
        p = parse_condition(text)
        assert p.action == "limit"
        assert p.unit == direction

    # Pattern 4 – Limit X to Y
    def test_pattern4(self):
        p = parse_condition("Limit complexity to 0.5")
        assert p.action == "limit"
        assert p.target == "complexity"
        assert p.value == pytest.approx(0.5)

    # Pattern 5 – Set X Y (without 'to')
    def test_pattern5(self):
        p = parse_condition("Set complexity 0.3")
        assert p.action == "set"
        assert p.target == "complexity"
        assert p.value == pytest.approx(0.3)

    # Unknown
    def test_unknown_condition(self):
        p = parse_condition("Do something weird")
        assert p.action == "unknown"
        assert p.target == "unknown"
        assert p.original == "Do something weird"


class TestNormalizeTarget:
    @pytest.mark.parametrize("inp,expected", [
        ("complexity", "complexity"),
        ("risk", "risk_score"),
        ("risk_score", "risk_score"),
        ("coherence", "coherence"),
        ("monitoring", "monitoring_duration"),
        ("monitor", "monitoring_duration"),
        ("duration", "monitoring_duration"),
        ("time", "monitoring_duration"),
        ("custom", "custom"),
    ])
    def test_normalize(self, inp, expected):
        assert _normalize_target(inp) == expected


class TestApplyCondition:
    @pytest.fixture()
    def mock_server(self):
        """Build a fake mcp_server with agent_metadata."""
        @dataclass
        class FakeMeta:
            status: str = "paused"
            dialectic_conditions: list = field(default_factory=list)
        server = MagicMock()
        server.agent_metadata = {"agent-1": FakeMeta()}
        server.load_metadata = MagicMock()
        server.load_metadata_async = AsyncMock()
        return server

    @pytest.mark.asyncio
    async def test_set_complexity(self, mock_server):
        pc = parse_condition("Set complexity to 0.3")
        result = await apply_condition(pc, "agent-1", mock_server)
        assert result["status"] == "applied"
        assert result["changes"]["complexity_limit"] == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_set_risk(self, mock_server):
        pc = parse_condition("Set risk to 0.5")
        result = await apply_condition(pc, "agent-1", mock_server)
        assert result["status"] == "applied"
        assert "risk_target" in result["changes"]

    @pytest.mark.asyncio
    async def test_set_coherence(self, mock_server):
        pc = parse_condition("Set coherence to 0.7")
        result = await apply_condition(pc, "agent-1", mock_server)
        assert result["status"] == "applied"
        assert "coherence_target" in result["changes"]

    @pytest.mark.asyncio
    async def test_monitor_hours(self, mock_server):
        pc = parse_condition("Monitor for 24 hours")
        result = await apply_condition(pc, "agent-1", mock_server)
        assert result["status"] == "applied"
        assert result["changes"]["monitoring_duration_hours"] == pytest.approx(24.0)

    @pytest.mark.asyncio
    async def test_monitor_minutes(self, mock_server):
        pc = parse_condition("Monitor for 60 minutes")
        result = await apply_condition(pc, "agent-1", mock_server)
        assert result["status"] == "applied"
        assert result["changes"]["monitoring_duration_hours"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_limit_condition(self, mock_server):
        pc = parse_condition("Keep risk below 0.5")
        result = await apply_condition(pc, "agent-1", mock_server)
        assert result["status"] == "applied"
        assert "risk_score_limit" in result["changes"]

    @pytest.mark.asyncio
    async def test_reduce_condition(self, mock_server):
        pc = parse_condition("Reduce complexity to 0.2")
        result = await apply_condition(pc, "agent-1", mock_server)
        assert result["status"] == "applied"
        assert "complexity_adjustment" in result["changes"]

    @pytest.mark.asyncio
    async def test_agent_not_found(self, mock_server):
        pc = parse_condition("Set complexity to 0.3")
        result = await apply_condition(pc, "nonexistent", mock_server)
        assert result["status"] == "failed"
        assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# 2. audit_log  (41 % -> higher)
# ---------------------------------------------------------------------------
from src.audit_log import AuditLogger, AuditEntry


class TestAuditEntry:
    def test_fields(self):
        e = AuditEntry(
            timestamp="2025-01-01T00:00:00",
            agent_id="a1",
            event_type="auto_attest",
            confidence=0.9,
            details={"k": "v"},
        )
        assert e.metadata is None


class TestAuditLogger:
    @pytest.fixture()
    def logger_inst(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNITARES_AUDIT_WRITE_JSONL", "1")
        log_file = tmp_path / "audit.jsonl"
        return AuditLogger(log_file=log_file)

    def test_log_lambda1_skip(self, logger_inst):
        logger_inst.log_lambda1_skip("a1", 0.4, 0.5, 10)
        entries = logger_inst.query_audit_log(event_type="lambda1_skip")
        assert len(entries) >= 1

    def test_log_auto_attest(self, logger_inst):
        logger_inst.log_auto_attest("a1", 0.9, True, 0.3, "approved")
        entries = logger_inst.query_audit_log(event_type="auto_attest")
        assert len(entries) >= 1

    def test_query_with_filters(self, logger_inst):
        logger_inst.log_auto_attest("a1", 0.9, True, 0.3, "ok")
        logger_inst.log_auto_attest("a2", 0.8, True, 0.2, "ok")
        assert len(logger_inst.query_audit_log(agent_id="a1")) >= 1
        assert len(logger_inst.query_audit_log(agent_id="a2")) >= 1

    def test_query_time_range(self, logger_inst):
        logger_inst.log_auto_attest("a1", 0.9, True, 0.3, "ok")
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        entries = logger_inst.query_audit_log(start_time=future)
        assert len(entries) == 0

    def test_get_skip_rate_metrics(self, logger_inst):
        for _ in range(3):
            logger_inst.log_lambda1_skip("a1", 0.3, 0.5, 1)
        for _ in range(7):
            logger_inst.log_auto_attest("a1", 0.9, True, 0.3, "ok")
        metrics = logger_inst.get_skip_rate_metrics("a1", window_hours=1)
        assert "skip_rate" in metrics
        assert "suspicious" in metrics

    def test_rotate_log(self, logger_inst):
        logger_inst.log_auto_attest("a1", 0.9, True, 0.3, "ok")
        result = logger_inst.rotate_log(max_age_days=0)
        # All entries should be archived (older than 0 days)
        assert result is not None



# ---------------------------------------------------------------------------
# 4. utils  (76 % -> higher)
# ---------------------------------------------------------------------------
from src.mcp_handlers.utils import (
    _infer_error_code_and_category,
    _sanitize_error_message,
    _make_json_serializable,
    error_response,
    format_metrics_report,
    format_metrics_text,
    generate_actionable_feedback,
)


class TestInferErrorCode:
    @pytest.mark.parametrize("msg,code,cat", [
        ("Agent not found", "NOT_FOUND", "validation_error"),
        ("Missing required parameter", "MISSING_REQUIRED", "validation_error"),
        ("Invalid value provided", "INVALID_PARAM", "validation_error"),
        ("Already exists in system", "ALREADY_EXISTS", "validation_error"),
        ("Too long for field", "VALUE_TOO_LARGE", "validation_error"),
        ("Permission denied", "PERMISSION_DENIED", "auth_error"),
        ("Agent is paused", "AGENT_PAUSED", "state_error"),
        ("Agent is archived", "AGENT_ARCHIVED", "state_error"),
        ("Request timed out", "TIMEOUT", "system_error"),
        ("Connection refused", "CONNECTION_ERROR", "system_error"),
        ("Database error occurred", "DATABASE_ERROR", "system_error"),
        ("Failed to process", "OPERATION_FAILED", "system_error"),
        ("Something completely unique", None, None),
    ])
    def test_inference(self, msg, code, cat):
        inferred_code, inferred_cat = _infer_error_code_and_category(msg)
        assert inferred_code == code
        assert inferred_cat == cat


class TestSanitizeErrorMessage:
    def test_removes_file_paths(self):
        msg = "Error at /Users/foo/bar/baz.py line 42"
        result = _sanitize_error_message(msg)
        assert "/Users/foo/bar" not in result

    def test_non_string_input(self):
        assert _sanitize_error_message(42) == "42"

    def test_cleans_internal_modules(self):
        msg = "src.mcp_handlers.utils.something failed"
        result = _sanitize_error_message(msg)
        assert "src.mcp_handlers.utils." not in result


class TestMakeJsonSerializable:
    def test_none(self):
        assert _make_json_serializable(None) is None

    def test_basic_types(self):
        assert _make_json_serializable("hello") == "hello"
        assert _make_json_serializable(42) == 42
        assert _make_json_serializable(3.14) == 3.14
        assert _make_json_serializable(True) is True

    def test_dict(self):
        assert _make_json_serializable({"a": 1}) == {"a": 1}

    def test_list(self):
        assert _make_json_serializable([1, 2, 3]) == [1, 2, 3]

    def test_set_conversion(self):
        result = _make_json_serializable({1, 2})
        assert isinstance(result, list)

    def test_datetime_conversion(self):
        dt = datetime(2025, 1, 1, 12, 0, 0)
        result = _make_json_serializable(dt)
        assert "2025-01-01" in result

    def test_enum_conversion(self):
        from enum import Enum
        class Color(Enum):
            RED = "red"
        assert _make_json_serializable(Color.RED) == "red"

    def test_large_list_truncation(self):
        big = list(range(200))
        result = _make_json_serializable(big)
        assert len(result) == 101  # 100 items + "... (100 more items)"

    def test_non_serializable_fallback(self):
        result = _make_json_serializable(object())
        assert isinstance(result, str)


class TestErrorResponse:
    def test_basic_error(self):
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None}):
            tc = error_response("Something went wrong")
        data = json.loads(tc.text)
        assert data["success"] is False
        assert "Something went wrong" in data["error"]

    def test_with_error_code(self):
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None}):
            tc = error_response("Not found", error_code="NOT_FOUND", error_category="validation_error")
        data = json.loads(tc.text)
        assert data["error_code"] == "NOT_FOUND"
        assert data["error_category"] == "validation_error"

    def test_auto_inferred_code(self):
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None}):
            tc = error_response("Agent not found")
        data = json.loads(tc.text)
        assert data.get("error_code") == "NOT_FOUND"

    def test_with_recovery(self):
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None}):
            tc = error_response("Bad", recovery={"action": "retry"})
        data = json.loads(tc.text)
        assert data["recovery"]["action"] == "retry"

    def test_with_details(self):
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None}):
            tc = error_response("Bad", details={"agent_id": "a1"})
        data = json.loads(tc.text)
        assert data["agent_id"] == "a1"

    def test_with_context(self):
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None}):
            tc = error_response("Bad", context={"tool": "test"})
        data = json.loads(tc.text)
        assert data["context"]["tool"] == "test"


class TestFormatMetricsReport:
    def test_basic_report(self):
        metrics = {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}
        report = format_metrics_report(metrics, "agent-1")
        assert report["agent_id"] == "agent-1"
        assert "timestamp" in report
        assert "eisv" in report
        assert report["eisv"]["E"] == 0.8

    def test_without_timestamp(self):
        report = format_metrics_report({}, "a1", include_timestamp=False)
        assert "timestamp" not in report

    def test_text_format(self):
        metrics = {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}
        text = format_metrics_report(metrics, "a1", format_style="text")
        assert isinstance(text, str)
        assert "Agent: a1" in text


class TestFormatMetricsText:
    def test_with_eisv(self):
        metrics = {"agent_id": "a1", "eisv": {"E": 0.8, "I": 0.9, "S": 0.1, "V": 0.0}}
        text = format_metrics_text(metrics)
        assert "EISV:" in text
        assert "Agent: a1" in text

    def test_with_flat_eisv(self):
        metrics = {"agent_id": "a1", "E": 0.8, "I": 0.9, "S": 0.1, "V": 0.0}
        text = format_metrics_text(metrics)
        assert "EISV:" in text

    def test_with_health(self):
        metrics = {"agent_id": "a1", "health_status": "healthy"}
        text = format_metrics_text(metrics)
        assert "Health: healthy" in text

    def test_with_key_metrics(self):
        metrics = {"agent_id": "a1", "coherence": 0.85, "risk_score": 0.2}
        text = format_metrics_text(metrics)
        assert "coherence:" in text
        assert "risk_score:" in text


class TestGenerateActionableFeedback:
    def test_no_feedback_first_update(self):
        metrics = {"coherence": 0.5, "updates": 1}
        fb = generate_actionable_feedback(metrics)
        # First update should skip coherence feedback
        coherence_fb = [f for f in fb if "oherence" in f]
        assert len(coherence_fb) == 0

    def test_high_risk_feedback(self):
        metrics = {"risk_score": 0.8, "updates": 5}
        fb = generate_actionable_feedback(metrics)
        assert any("complexity" in f.lower() or "risk" in f.lower() for f in fb)

    def test_void_high_energy_low_integrity(self):
        metrics = {"void_active": True, "E": 0.8, "I": 0.3, "updates": 5}
        fb = generate_actionable_feedback(metrics)
        assert any("void" in f.lower() for f in fb)

    def test_void_high_integrity_low_energy(self):
        metrics = {"void_active": True, "E": 0.3, "I": 0.8, "updates": 5}
        fb = generate_actionable_feedback(metrics)
        assert any("void" in f.lower() for f in fb)

    def test_void_generic(self):
        metrics = {"void_active": True, "E": 0.5, "I": 0.5, "updates": 5}
        fb = generate_actionable_feedback(metrics)
        assert any("void" in f.lower() for f in fb)

    def test_response_text_confusion(self):
        metrics = {"updates": 5}
        fb = generate_actionable_feedback(metrics, response_text="I'm not sure about this")
        assert any("uncertainty" in f.lower() or "not sure" in f.lower() for f in fb)

    def test_response_text_overconfidence_low_coherence(self):
        metrics = {"coherence": 0.4, "updates": 5}
        fb = generate_actionable_feedback(
            metrics, response_text="This is definitely the right answer"
        )
        assert any("confidence" in f.lower() or "assumptions" in f.lower() for f in fb)

    def test_exploration_low_coherence_drop(self):
        metrics = {"coherence": 0.2, "regime": "exploration", "updates": 5}
        fb = generate_actionable_feedback(metrics, previous_coherence=0.5)
        assert any("exploration" in f.lower() or "coherence" in f.lower() for f in fb)

    def test_stable_coherence_drop(self):
        metrics = {"coherence": 0.5, "regime": "locked", "updates": 5}
        fb = generate_actionable_feedback(metrics, previous_coherence=0.8)
        assert any("drop" in f.lower() or "coherence" in f.lower() for f in fb)

    def test_convergent_task_low_coherence(self):
        metrics = {"coherence": 0.3, "regime": "transition", "updates": 5}
        fb = generate_actionable_feedback(metrics, task_type="convergent")
        assert any("coherence" in f.lower() or "convergent" in f.lower() for f in fb)

    def test_moderate_risk_degraded(self):
        metrics = {"risk_score": 0.6, "updates": 5}
        fb = generate_actionable_feedback(
            metrics, interpreted_state={"health": "degraded", "mode": "unknown", "basin": "unknown"}
        )
        assert any("checkpoint" in f.lower() or "complexity" in f.lower() for f in fb)


# ---------------------------------------------------------------------------
# 5. llm_delegation  (12 % -> higher)
# ---------------------------------------------------------------------------
from src.mcp_handlers.support.llm_delegation import (
    _get_default_model,
    _wants_reasoning_effort_none,
    call_local_llm,
    synthesize_results,
    explain_anomaly,
    generate_recovery_coaching,
    generate_antithesis,
    generate_synthesis,
    run_full_dialectic,
    is_llm_available,
)


class TestGetDefaultModel:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("UNITARES_LLM_MODEL", raising=False)
        assert _get_default_model() == "gemma4:latest"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("UNITARES_LLM_MODEL", "custom:7b")
        assert _get_default_model() == "custom:7b"


class TestCallLocalLLM:
    @pytest.mark.asyncio
    async def test_returns_none_when_openai_unavailable(self):
        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", False):
            result = await call_local_llm("test prompt")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_client_unavailable(self):
        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.llm_delegation._get_ollama_client", return_value=None):
            result = await call_local_llm("test prompt")
            assert result is None

    @pytest.mark.asyncio
    async def test_successful_call(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="test response"))]
        mock_client.chat.completions.create.return_value = mock_response

        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.llm_delegation._get_ollama_client", return_value=mock_client):
            result = await call_local_llm("test prompt", model="test-model")
            assert result == "test response"

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """When the LLM call times out, call_local_llm returns None.

        The implementation wraps the executor call in
        ``asyncio.wait_for(..., timeout=timeout + 5)`` and catches
        ``asyncio.TimeoutError``. Pre-2026-05-06 this test used
        ``time.sleep(5)`` inside the mock to make ``wait_for`` actually fire
        — but with the +5 buffer that meant the test ran for the full 5
        seconds on every invocation. We exercise the same except-clause by
        raising ``asyncio.TimeoutError`` directly from the mocked OpenAI
        call, which propagates out of ``run_in_executor`` and ``wait_for``
        unchanged. Same exception class, same handler branch — without the
        artificial 5s wait.
        """
        import asyncio as _asyncio

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _asyncio.TimeoutError("simulated timeout")

        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.llm_delegation._get_ollama_client", return_value=mock_client):
            result = await call_local_llm("test", timeout=0.01)
            assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("boom")

        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.llm_delegation._get_ollama_client", return_value=mock_client):
            result = await call_local_llm("test")
            assert result is None

    @pytest.mark.asyncio
    async def test_qwen3_injects_reasoning_effort_none(self):
        """qwen3.x models must receive reasoning.effort=none so thinking-mode
        output isn't hidden by /v1/chat/completions."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_client.chat.completions.create.return_value = mock_response

        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.llm_delegation._get_ollama_client", return_value=mock_client):
            await call_local_llm("test", model="qwen3.6:27b-coding-nvfp4")

        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert kwargs.get("extra_body") == {"reasoning": {"effort": "none"}}

    @pytest.mark.asyncio
    async def test_gemma4_does_not_inject_reasoning(self):
        """gemma4 and other non-thinking models must not receive extra_body,
        which would be a no-op at best and a 400 at worst on some backends."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
        mock_client.chat.completions.create.return_value = mock_response

        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.llm_delegation._get_ollama_client", return_value=mock_client):
            await call_local_llm("test", model="gemma4:latest")

        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert "extra_body" not in kwargs


class TestWantsReasoningEffortNone:
    @pytest.mark.parametrize("model", [
        "qwen3.6:27b-coding-nvfp4",
        "qwen3.6:27b-coding-mxfp8",
        "qwen3-coder-next:latest",
        "Qwen3-Next-80B",
        "qwen-3.6-27b",
    ])
    def test_qwen3_family_opts_in(self, model):
        assert _wants_reasoning_effort_none(model) is True

    @pytest.mark.parametrize("model", [
        "gemma4:latest",
        "qwen2.5:7b",
        "deepseek-r1:14b",
        "llama3:8b",
        "",
        None,
    ])
    def test_other_families_opt_out(self, model):
        assert _wants_reasoning_effort_none(model) is False


class TestSynthesizeResults:
    @pytest.mark.asyncio
    async def test_empty_discoveries_returns_none(self):
        result = await synthesize_results([])
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_synthesis(self):
        discoveries = [
            {"summary": "Found error pattern A", "type": "insight"},
            {"summary": "Found error pattern B", "type": "anomaly"},
        ]
        with patch("src.mcp_handlers.support.llm_delegation.call_local_llm", new_callable=AsyncMock, return_value="Key insight: patterns A and B are related"):
            result = await synthesize_results(discoveries, query="errors")
            assert result is not None
            assert result["text"] == "Key insight: patterns A and B are related"
            assert result["discoveries_analyzed"] == 2
            assert result["query"] == "errors"

    @pytest.mark.asyncio
    async def test_llm_unavailable_returns_none(self):
        with patch("src.mcp_handlers.support.llm_delegation.call_local_llm", new_callable=AsyncMock, return_value=None):
            result = await synthesize_results([{"summary": "test", "type": "t"}])
            assert result is None


class TestExplainAnomaly:
    @pytest.mark.asyncio
    async def test_successful_explanation(self):
        with patch("src.mcp_handlers.support.llm_delegation.call_local_llm", new_callable=AsyncMock, return_value="Root cause: X"):
            result = await explain_anomaly("agent-1", "risk_spike", "Risk spiked to 0.9")
            assert result == "Root cause: X"

    @pytest.mark.asyncio
    async def test_with_metrics(self):
        with patch("src.mcp_handlers.support.llm_delegation.call_local_llm", new_callable=AsyncMock, return_value="ok") as mock_call:
            await explain_anomaly("agent-1", "risk_spike", "desc", metrics={"E": 0.8})
            prompt = mock_call.call_args[1]["prompt"]
            assert "E" in prompt


class TestGenerateRecoveryCoaching:
    @pytest.mark.asyncio
    async def test_basic_coaching(self):
        with patch("src.mcp_handlers.support.llm_delegation.call_local_llm", new_callable=AsyncMock, return_value="Focus on X"):
            result = await generate_recovery_coaching("agent-1", ["blocker A", "blocker B"])
            assert result == "Focus on X"

    @pytest.mark.asyncio
    async def test_with_state(self):
        state = {"eisv": {"E": 0.8, "I": 0.7, "S": 0.1, "V": 0.0}}
        with patch("src.mcp_handlers.support.llm_delegation.call_local_llm", new_callable=AsyncMock, return_value="ok") as mock_call:
            await generate_recovery_coaching("agent-1", ["b1"], current_state=state)
            prompt = mock_call.call_args[1]["prompt"]
            assert "EISV" in prompt


_MOD = "src.mcp_handlers.support.llm_delegation"


class TestGenerateAntithesis:
    @pytest.mark.asyncio
    async def test_successful_antithesis(self):
        """Structured reviewer path yields list concerns + structured flag."""
        structured = {
            "concerns": ["underestimates risk", "ignores trajectory"],
            "counter_reasoning": "An alternative explanation",
            "grounding_cited": "coherence trend",
            "position": "dispute",
            "suggested_conditions": ["Add monitoring for 48h"],
        }
        thesis = {
            "root_cause": "High complexity",
            "proposed_conditions": ["Reduce complexity to 0.3"],
            "reasoning": "I believe...",
        }
        with patch(f"{_MOD}.call_local_llm_structured", new_callable=AsyncMock, return_value=structured):
            result = await generate_antithesis(thesis)
            assert result is not None
            assert result["source"] == "llm_synthetic_reviewer"
            assert result["_structured"] is True
            assert isinstance(result["concerns"], list) and len(result["concerns"]) == 2
            assert result["counter_reasoning"]
            assert result["suggested_conditions"] == ["Add monitoring for 48h"]

    @pytest.mark.asyncio
    async def test_llm_unavailable(self):
        """Both structured and free-text unavailable -> None."""
        with patch(f"{_MOD}.call_local_llm_structured", new_callable=AsyncMock, return_value=None), \
             patch(f"{_MOD}.call_local_llm", new_callable=AsyncMock, return_value=None):
            result = await generate_antithesis({"root_cause": "x"})
            assert result is None

    @pytest.mark.asyncio
    async def test_with_agent_state(self):
        """agent_state EISV signals are passed into the reviewer prompt."""
        with patch(f"{_MOD}.call_local_llm_structured", new_callable=AsyncMock,
                   return_value={"concerns": ["c1", "c2"], "counter_reasoning": "x",
                                 "grounding_cited": "g", "position": "refine",
                                 "suggested_conditions": []}) as mock_call:
            await generate_antithesis(
                {"root_cause": "x", "proposed_conditions": [], "reasoning": "r"},
                agent_state={"risk_score": 0.8, "coherence": 0.3},
            )
            user_msg = mock_call.call_args[1]["messages"][1]["content"]
            assert "risk_score" in user_msg


class TestGenerateSynthesis:
    @pytest.mark.asyncio
    async def test_successful_synthesis(self):
        structured = {
            "agreed_root_cause": "Both sides agree on high complexity",
            "reasoning": "Agent demonstrated understanding",
            "merged_conditions": ["Reduce complexity", "monitor 24h"],
            "recommendation": "RESUME",
        }
        thesis = {"root_cause": "x", "proposed_conditions": ["c1"], "reasoning": "r"}
        antithesis = {"concerns": ["c"], "counter_reasoning": "cr", "suggested_conditions": ["sc"]}
        with patch(f"{_MOD}.call_local_llm_structured", new_callable=AsyncMock, return_value=structured):
            result = await generate_synthesis(thesis, antithesis, synthesis_round=1)
            assert result is not None
            assert result["recommendation"] == "RESUME"
            assert result["synthesis_round"] == 1
            assert isinstance(result["merged_conditions"], list)

    @pytest.mark.asyncio
    async def test_cooldown_recommendation(self):
        structured = {"agreed_root_cause": "x", "reasoning": "needs time",
                      "merged_conditions": [], "recommendation": "COOLDOWN"}
        with patch(f"{_MOD}.call_local_llm_structured", new_callable=AsyncMock, return_value=structured):
            result = await generate_synthesis({}, {})
            assert result["recommendation"] == "COOLDOWN"

    @pytest.mark.asyncio
    async def test_escalate_recommendation(self):
        structured = {"agreed_root_cause": "x", "reasoning": "needs human",
                      "merged_conditions": [], "recommendation": "ESCALATE"}
        with patch(f"{_MOD}.call_local_llm_structured", new_callable=AsyncMock, return_value=structured):
            result = await generate_synthesis({}, {})
            assert result["recommendation"] == "ESCALATE"

    @pytest.mark.asyncio
    async def test_fallback_to_free_text_defaults_escalate(self):
        """Structured unavailable -> free-text fallback, ESCALATE default."""
        with patch(f"{_MOD}.call_local_llm_structured", new_callable=AsyncMock, return_value=None), \
             patch(f"{_MOD}.call_local_llm", new_callable=AsyncMock, return_value="ambivalent"):
            result = await generate_synthesis({}, {"concerns": ["a", "b"]})
            assert result["recommendation"] == "ESCALATE"
            assert result["_degraded"] is True


class TestRunFullDialectic:
    @pytest.mark.asyncio
    async def test_successful_full_dialectic(self):
        antithesis = {"concerns": "x", "source": "llm_synthetic_reviewer"}
        synthesis = {"recommendation": "RESUME", "source": "llm_synthesis"}
        with patch("src.mcp_handlers.support.llm_delegation.generate_antithesis", new_callable=AsyncMock, return_value=antithesis), \
             patch("src.mcp_handlers.support.llm_delegation.generate_synthesis", new_callable=AsyncMock, return_value=synthesis):
            result = await run_full_dialectic({"root_cause": "x", "proposed_conditions": ["c"]})
            assert result["success"] is True
            assert result["recommendation"] == "RESUME"

    @pytest.mark.asyncio
    async def test_antithesis_fails(self):
        with patch("src.mcp_handlers.support.llm_delegation.generate_antithesis", new_callable=AsyncMock, return_value=None):
            result = await run_full_dialectic({"root_cause": "x"})
            assert result["success"] is False
            assert "antithesis" in result["error"]

    @pytest.mark.asyncio
    async def test_synthesis_fails(self):
        with patch("src.mcp_handlers.support.llm_delegation.generate_antithesis", new_callable=AsyncMock, return_value={"concerns": "x"}), \
             patch("src.mcp_handlers.support.llm_delegation.generate_synthesis", new_callable=AsyncMock, return_value=None):
            result = await run_full_dialectic({"root_cause": "x"})
            assert result["success"] is False
            assert "synthesis" in result["error"]


class TestIsLLMAvailable:
    @pytest.mark.asyncio
    async def test_not_available_no_sdk(self):
        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", False):
            assert await is_llm_available() is False

    @pytest.mark.asyncio
    async def test_not_available_no_client(self):
        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.llm_delegation._get_ollama_client", return_value=None):
            assert await is_llm_available() is False

    @pytest.mark.asyncio
    async def test_available(self):
        mock_client = MagicMock()
        mock_client.models.list.return_value = []
        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.llm_delegation._get_ollama_client", return_value=mock_client):
            assert await is_llm_available() is True

    @pytest.mark.asyncio
    async def test_ping_fails(self):
        mock_client = MagicMock()
        mock_client.models.list.side_effect = RuntimeError("down")
        with patch("src.mcp_handlers.support.llm_delegation.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.llm_delegation._get_ollama_client", return_value=mock_client):
            assert await is_llm_available() is False


# ---------------------------------------------------------------------------
# 6. model_inference  (8 % -> higher)
# ---------------------------------------------------------------------------
from src.mcp_handlers.support.model_inference import handle_call_model


class TestHandleCallModel:
    @pytest.mark.asyncio
    async def test_missing_openai_sdk(self):
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", False):
            result = await handle_call_model({"prompt": "test"})
            assert isinstance(result, list)
            data = json.loads(result[0].text)
            assert data["success"] is False
            assert "DEPENDENCY_MISSING" in data.get("error_code", "")

    @pytest.mark.asyncio
    async def test_missing_prompt(self):
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True):
            result = await handle_call_model({})
            assert isinstance(result, list)
            data = json.loads(result[0].text)
            assert data["success"] is False

    @pytest.mark.asyncio
    async def test_ollama_routing(self):
        mock_client_cls = MagicMock()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="response text"))]
        mock_response.usage = MagicMock(total_tokens=50)
        mock_response.model = "llama3:70b"
        mock_client.chat.completions.create.return_value = mock_response
        mock_client_cls.return_value = mock_client

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", mock_client_cls, create=True):
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })
            data = json.loads(result[0].text)
            assert data["success"] is True
            assert data["routed_via"] == "ollama"

    @pytest.mark.asyncio
    async def test_hf_missing_token(self):
        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in ("HF_TOKEN", "HUGGINGFACE_TOKEN")
        }
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch.dict(os.environ, env_clean, clear=True):
            result = await handle_call_model({
                "prompt": "test",
                "provider": "hf",
                "privacy": "cloud",
            })
            data = json.loads(result[0].text)
            assert data["success"] is False

    @pytest.mark.asyncio
    async def test_unknown_provider_rejected(self):
        # Gemini and ngrok.ai providers were removed. Pydantic blocks unknown
        # values at the MCP boundary; direct callers get INVALID_PROVIDER.
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True):
            result = await handle_call_model({
                "prompt": "test",
                "provider": "custom",
                "privacy": "cloud",
            })
            data = json.loads(result[0].text)
            assert data["success"] is False
            assert data.get("error_code") == "INVALID_PROVIDER"

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.chat.completions.create.side_effect = RuntimeError("timeout error")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", mock_client_cls, create=True):
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })
            data = json.loads(result[0].text)
            assert data["success"] is False
            assert data["error_code"] == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_rate_limit_error_code(self):
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.chat.completions.create.side_effect = RuntimeError("rate limit exceeded")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", mock_client_cls, create=True):
            result = await handle_call_model({"prompt": "t", "provider": "ollama"})
            data = json.loads(result[0].text)
            assert data["error_code"] == "RATE_LIMIT_EXCEEDED"

    @pytest.mark.asyncio
    async def test_model_not_found_error_code(self):
        mock_client_cls = MagicMock()
        mock_client_cls.return_value.chat.completions.create.side_effect = RuntimeError("model not found")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", mock_client_cls, create=True):
            result = await handle_call_model({"prompt": "t", "provider": "ollama"})
            data = json.loads(result[0].text)
            assert data["error_code"] == "MODEL_NOT_AVAILABLE"


# ---------------------------------------------------------------------------
# 7. dialectic_reviewer  (10 % -> higher)
# ---------------------------------------------------------------------------
from src.mcp_handlers.dialectic.reviewer import (
    _has_recently_reviewed,
    is_agent_in_active_session,
    select_reviewer,
)


class TestHasRecentlyReviewed:
    @pytest.mark.asyncio
    async def test_pg_returns_false(self):
        with patch("src.mcp_handlers.dialectic.reviewer.pg_has_recently_reviewed",
                    new_callable=AsyncMock, return_value=False):
            assert await _has_recently_reviewed("r1", "p1") is False

    @pytest.mark.asyncio
    async def test_pg_returns_true(self):
        with patch("src.mcp_handlers.dialectic.reviewer.pg_has_recently_reviewed",
                    new_callable=AsyncMock, return_value=True):
            assert await _has_recently_reviewed("r1", "p1") is True

    @pytest.mark.asyncio
    async def test_pg_fails_fallback_disk(self):
        with patch("src.mcp_handlers.dialectic.reviewer.pg_has_recently_reviewed",
                    new_callable=AsyncMock, side_effect=RuntimeError("db down")), \
             patch("src.mcp_handlers.dialectic.reviewer.SESSION_STORAGE_DIR",
                    Path("/tmp/nonexistent_dir_test_xyz")):
            result = await _has_recently_reviewed("r1", "p1")
            assert result is False


class TestIsAgentInActiveSession:
    @pytest.mark.asyncio
    async def test_pg_says_true(self):
        with patch("src.mcp_handlers.dialectic.reviewer.pg_is_agent_in_active_session",
                    new_callable=AsyncMock, return_value=True), \
             patch("src.mcp_handlers.dialectic.auto_resolve.check_and_resolve_stuck_sessions",
                    new_callable=AsyncMock, return_value={"resolved_count": 0}):
            result = await is_agent_in_active_session("agent-1")
            assert result is True

    @pytest.mark.asyncio
    async def test_pg_says_false(self):
        with patch("src.mcp_handlers.dialectic.reviewer.pg_is_agent_in_active_session",
                    new_callable=AsyncMock, return_value=False), \
             patch("src.mcp_handlers.dialectic.auto_resolve.check_and_resolve_stuck_sessions",
                    new_callable=AsyncMock, return_value={"resolved_count": 0}), \
             patch("src.mcp_handlers.dialectic.reviewer.ACTIVE_SESSIONS", {}):
            result = await is_agent_in_active_session("agent-1")
            assert result is False


class TestSelectReviewer:
    @pytest.fixture(autouse=True)
    def _enable_autoselect(self, monkeypatch):
        # Existing coverage asserts the ranking / filtering behavior of
        # select_reviewer; the feature is gated off by default as of the
        # ghost-reviewer fix, so these tests opt in via the env flag.
        monkeypatch.setenv("UNITARES_AUTOSELECT_REVIEWER", "1")

    @pytest.mark.asyncio
    async def test_no_metadata(self):
        result = await select_reviewer("agent-1", metadata=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_metadata(self):
        result = await select_reviewer("agent-1", metadata={})
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_by_default(self, monkeypatch):
        # Overrides the class-level autouse fixture for this case.
        monkeypatch.delenv("UNITARES_AUTOSELECT_REVIEWER", raising=False)
        metadata = {
            "agent-1": {"status": "active"},
            "agent-2": {"status": "active"},
        }
        # No patches on is_agent_in_active_session / _has_recently_reviewed —
        # the gate must short-circuit before any candidate inspection happens.
        result = await select_reviewer("agent-1", metadata=metadata)
        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    async def test_disabled_when_flag_falsy(self, monkeypatch, value):
        monkeypatch.setenv("UNITARES_AUTOSELECT_REVIEWER", value)
        metadata = {
            "agent-1": {"status": "active"},
            "agent-2": {"status": "active"},
        }
        result = await select_reviewer("agent-1", metadata=metadata)
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_paused_agent(self):
        metadata = {
            "agent-1": {"status": "active"},
            "agent-2": {"status": "active"},
        }
        with patch("src.mcp_handlers.dialectic.reviewer.is_agent_in_active_session",
                    new_callable=AsyncMock, return_value=False), \
             patch("src.mcp_handlers.dialectic.reviewer._has_recently_reviewed",
                    new_callable=AsyncMock, return_value=False):
            result = await select_reviewer("agent-1", metadata=metadata)
            assert result == "agent-2"

    @pytest.mark.asyncio
    async def test_skips_excluded(self):
        metadata = {
            "agent-1": {"status": "active"},
            "agent-2": {"status": "active"},
            "agent-3": {"status": "active"},
        }
        with patch("src.mcp_handlers.dialectic.reviewer.is_agent_in_active_session",
                    new_callable=AsyncMock, return_value=False), \
             patch("src.mcp_handlers.dialectic.reviewer._has_recently_reviewed",
                    new_callable=AsyncMock, return_value=False):
            result = await select_reviewer("agent-1", metadata=metadata,
                                           exclude_agent_ids=["agent-2"])
            assert result == "agent-3"

    @pytest.mark.asyncio
    async def test_skips_inactive(self):
        metadata = {
            "agent-1": {"status": "active"},
            "agent-2": {"status": "archived"},
        }
        with patch("src.mcp_handlers.dialectic.reviewer.is_agent_in_active_session",
                    new_callable=AsyncMock, return_value=False), \
             patch("src.mcp_handlers.dialectic.reviewer._has_recently_reviewed",
                    new_callable=AsyncMock, return_value=False):
            result = await select_reviewer("agent-1", metadata=metadata)
            assert result is None

    @pytest.mark.asyncio
    async def test_skips_string_meta(self):
        metadata = {
            "agent-1": {"status": "active"},
            "agent-2": "invalid-string",
        }
        with patch("src.mcp_handlers.dialectic.reviewer.is_agent_in_active_session",
                    new_callable=AsyncMock, return_value=False), \
             patch("src.mcp_handlers.dialectic.reviewer._has_recently_reviewed",
                    new_callable=AsyncMock, return_value=False):
            result = await select_reviewer("agent-1", metadata=metadata)
            assert result is None

    @pytest.mark.asyncio
    async def test_no_eligible_candidates(self):
        metadata = {
            "agent-1": {"status": "active"},
            "agent-2": {"status": "active"},
        }
        with patch("src.mcp_handlers.dialectic.reviewer.is_agent_in_active_session",
                    new_callable=AsyncMock, return_value=True), \
             patch("src.mcp_handlers.dialectic.reviewer._has_recently_reviewed",
                    new_callable=AsyncMock, return_value=False):
            result = await select_reviewer("agent-1", metadata=metadata)
            assert result is None


# ---------------------------------------------------------------------------
# 8. dialectic_calibration  (45 % -> higher)
# ---------------------------------------------------------------------------
from src.mcp_handlers.dialectic.calibration import (
    update_calibration_from_dialectic,
    update_calibration_from_dialectic_disagreement,
    backfill_calibration_from_historical_sessions,
)


def _make_mock_session(dispute_type="verification", discovery_id=None, phase="resolved"):
    session = MagicMock()
    session.session_id = "sess-1"
    session.dispute_type = dispute_type
    session.paused_agent_id = "agent-1"
    session.discovery_id = discovery_id
    session.created_at = datetime.now()
    session.resolution = None
    session.phase = MagicMock()
    session.phase.value = phase
    return session


class TestUpdateCalibrationFromDialectic:
    @pytest.mark.asyncio
    async def test_no_resolution(self):
        session = _make_mock_session()
        session.resolution = None
        result = await update_calibration_from_dialectic(session, resolution=None)
        assert result is False

    @pytest.mark.asyncio
    async def test_wrong_dispute_type(self):
        session = _make_mock_session(dispute_type="dispute")
        resolution = MagicMock(action="resume")
        result = await update_calibration_from_dialectic(session, resolution=resolution)
        assert result is False

    @pytest.mark.asyncio
    async def test_successful_update(self):
        session = _make_mock_session(dispute_type="verification", discovery_id="disc-1")
        resolution = MagicMock(action="resume")
        audit_entries = [{"discovery_id": "disc-1", "confidence": 0.85, "complexity_discrepancy": 0.1}]

        mock_server = MagicMock()
        mock_checker = MagicMock()

        with patch("src.mcp_handlers.dialectic.calibration.mcp_server", mock_server), \
             patch("src.mcp_handlers.dialectic.calibration.audit_logger") as mock_audit, \
             patch("src.mcp_handlers.dialectic.calibration.calibration_checker", mock_checker):
            mock_audit.query_audit_log.return_value = audit_entries
            result = await update_calibration_from_dialectic(session, resolution=resolution)
            assert result is True
            mock_checker.record_prediction.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_confidence_in_audit(self):
        session = _make_mock_session(dispute_type="verification", discovery_id="disc-1")
        resolution = MagicMock(action="resume")

        mock_server = MagicMock()
        with patch("src.mcp_handlers.dialectic.calibration.mcp_server", mock_server), \
             patch("src.mcp_handlers.dialectic.calibration.audit_logger") as mock_audit:
            mock_audit.query_audit_log.return_value = []
            result = await update_calibration_from_dialectic(session, resolution=resolution)
            assert result is False

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        session = _make_mock_session(dispute_type="verification")
        resolution = MagicMock(action="resume")
        with patch("src.mcp_handlers.shared.get_mcp_server", side_effect=RuntimeError("boom")):
            result = await update_calibration_from_dialectic(session, resolution=resolution)
            assert result is False


class TestUpdateCalibrationFromDisagreement:
    @pytest.mark.asyncio
    async def test_wrong_dispute_type(self):
        session = _make_mock_session(dispute_type="dispute")
        assert await update_calibration_from_dialectic_disagreement(session) is False

    @pytest.mark.asyncio
    async def test_no_confidence(self):
        session = _make_mock_session(dispute_type="verification", discovery_id="d1")
        mock_server = MagicMock()
        with patch("src.mcp_handlers.dialectic.calibration.mcp_server", mock_server), \
             patch("src.mcp_handlers.dialectic.calibration.audit_logger") as mock_audit:
            mock_audit.query_audit_log.return_value = []
            assert await update_calibration_from_dialectic_disagreement(session) is False

    @pytest.mark.asyncio
    async def test_successful_disagreement_log(self):
        session = _make_mock_session(dispute_type="verification", discovery_id="d1")
        entries = [{"discovery_id": "d1", "confidence": 0.9, "complexity_discrepancy": 0.2}]
        mock_server = MagicMock()
        with patch("src.mcp_handlers.dialectic.calibration.mcp_server", mock_server), \
             patch("src.mcp_handlers.dialectic.calibration.audit_logger") as mock_audit:
            mock_audit.query_audit_log.return_value = entries
            result = await update_calibration_from_dialectic_disagreement(session)
            assert result is True

    @pytest.mark.asyncio
    async def test_timestamp_match(self):
        session = _make_mock_session(dispute_type="verification", discovery_id=None)
        now = datetime.now()
        session.created_at = now
        entries = [{"timestamp": now.isoformat(), "confidence": 0.8, "complexity_discrepancy": 0.1}]
        mock_server = MagicMock()
        with patch("src.mcp_handlers.dialectic.calibration.mcp_server", mock_server), \
             patch("src.mcp_handlers.dialectic.calibration.audit_logger") as mock_audit:
            mock_audit.query_audit_log.return_value = entries
            result = await update_calibration_from_dialectic_disagreement(session)
            assert result is True

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        session = _make_mock_session(dispute_type="verification")
        with patch("src.mcp_handlers.shared.get_mcp_server", side_effect=RuntimeError("boom")):
            result = await update_calibration_from_dialectic_disagreement(session)
            assert result is False


class TestBackfillCalibration:
    @pytest.mark.asyncio
    async def test_no_session_files(self):
        with patch("src.mcp_handlers.dialectic.calibration.SESSION_STORAGE_DIR", Path("/tmp/empty_test_dir_xyz")):
            result = await backfill_calibration_from_historical_sessions()
            assert result["processed"] == 0

    @pytest.mark.asyncio
    async def test_with_sessions(self, tmp_path):
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        sess_file = session_dir / "sess-1.json"
        sess_file.write_text(json.dumps({"session_id": "sess-1"}))

        mock_session = _make_mock_session(dispute_type="verification", phase="resolved")
        mock_session.resolution = MagicMock(action="resume")

        with patch("src.mcp_handlers.dialectic.calibration.SESSION_STORAGE_DIR", session_dir), \
             patch("src.mcp_handlers.dialectic.calibration.load_session", new_callable=AsyncMock, return_value=mock_session), \
             patch("src.mcp_handlers.dialectic.calibration.update_calibration_from_dialectic", new_callable=AsyncMock, return_value=True):
            result = await backfill_calibration_from_historical_sessions()
            assert result["processed"] >= 1
            assert result["updated"] >= 1


# ---------------------------------------------------------------------------
# 9. dialectic_resolution  (12 % -> higher)
# ---------------------------------------------------------------------------
from src.mcp_handlers.dialectic.resolution import execute_resolution


@dataclass
class _FakeMetaForResolution:
    status: str = "paused"
    paused_at: Optional[str] = "2025-01-01"
    dialectic_conditions: list = field(default_factory=list)
    def add_lifecycle_event(self, event_type, details):
        pass


class TestExecuteResolution:
    def _make_session_and_resolution(self, status="paused", discovery_id=None, dispute_type="verification"):
        session = MagicMock()
        session.paused_agent_id = "agent-1"
        session.session_id = "sess-1"
        session.discovery_id = discovery_id
        session.dispute_type = dispute_type

        resolution = MagicMock()
        resolution.action = "resume"
        resolution.conditions = ["Set complexity to 0.3"]
        resolution.root_cause = "High complexity"
        resolution.hash.return_value = "abc123"

        meta = _FakeMetaForResolution(status=status)
        mock_server = MagicMock()
        mock_server.agent_metadata = {"agent-1": meta}
        mock_server.load_metadata = MagicMock()
        mock_server.load_metadata_async = AsyncMock()

        return session, resolution, mock_server

    @pytest.mark.asyncio
    async def test_successful_resolution(self):
        session, resolution, mock_server = self._make_session_and_resolution()
        with patch("src.mcp_handlers.dialectic.resolution.mcp_server", mock_server), \
             patch("src.mcp_handlers.dialectic.resolution.parse_condition") as mock_parse, \
             patch("src.mcp_handlers.dialectic.resolution.apply_condition", new_callable=AsyncMock, return_value={"status": "applied"}), \
             patch("src.agent_storage.update_agent", new_callable=AsyncMock, return_value=None):
            mock_parse.return_value = ParsedCondition("set", "complexity", 0.3)
            result = await execute_resolution(session, resolution)
            assert result["success"] is True
            assert result["new_status"] == "active"

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        session, resolution, mock_server = self._make_session_and_resolution()
        mock_server.agent_metadata = {}
        with patch("src.mcp_handlers.dialectic.resolution.mcp_server", mock_server):
            with pytest.raises(ValueError, match="not found"):
                await execute_resolution(session, resolution)

    @pytest.mark.asyncio
    async def test_agent_not_paused(self):
        session, resolution, mock_server = self._make_session_and_resolution(status="active")
        with patch("src.mcp_handlers.dialectic.resolution.mcp_server", mock_server):
            result = await execute_resolution(session, resolution)
            assert result["success"] is False
            assert "not 'paused'" in result["warning"]

    @pytest.mark.asyncio
    async def test_condition_apply_failure(self):
        session, resolution, mock_server = self._make_session_and_resolution()
        with patch("src.mcp_handlers.dialectic.resolution.mcp_server", mock_server), \
             patch("src.mcp_handlers.dialectic.resolution.parse_condition", side_effect=RuntimeError("parse error")), \
             patch("src.agent_storage.update_agent", new_callable=AsyncMock, return_value=None):
            result = await execute_resolution(session, resolution)
            assert result["success"] is True
            assert any(c.get("status") == "failed" for c in result["applied_conditions"])


# ---------------------------------------------------------------------------
# 10. middleware  (82 % -> higher)
# ---------------------------------------------------------------------------
from src.mcp_handlers.middleware import (
    DispatchContext,
    unwrap_kwargs,
    resolve_alias,
    validate_params,
    check_rate_limit,
    _tool_call_history,
)


class TestDispatchContext:
    def test_defaults(self):
        ctx = DispatchContext()
        assert ctx.session_key is None
        assert ctx.bound_agent_id is None
        assert ctx.migration_note is None


class TestUnwrapKwargs:
    @pytest.mark.asyncio
    async def test_unwrap_dict_kwargs(self):
        ctx = DispatchContext()
        args = {"kwargs": {"prompt": "hello", "model": "test"}}
        name, result_args, _ = await unwrap_kwargs("call_model", args, ctx)
        assert "kwargs" not in result_args
        assert result_args["prompt"] == "hello"
        assert result_args["model"] == "test"

    @pytest.mark.asyncio
    async def test_unwrap_string_kwargs(self):
        ctx = DispatchContext()
        args = {"kwargs": '{"prompt": "hello"}'}
        name, result_args, _ = await unwrap_kwargs("call_model", args, ctx)
        assert "kwargs" not in result_args
        assert result_args["prompt"] == "hello"

    @pytest.mark.asyncio
    async def test_invalid_string_kwargs(self):
        ctx = DispatchContext()
        args = {"kwargs": "not json", "other": 1}
        name, result_args, _ = await unwrap_kwargs("call_model", args, ctx)
        assert "kwargs" in result_args  # not removed since parse failed

    @pytest.mark.asyncio
    async def test_no_kwargs(self):
        ctx = DispatchContext()
        args = {"prompt": "hello"}
        name, result_args, _ = await unwrap_kwargs("call_model", args, ctx)
        assert result_args["prompt"] == "hello"


class TestResolveAlias:
    @pytest.mark.asyncio
    async def test_no_alias(self):
        ctx = DispatchContext()
        with patch("src.mcp_handlers.tool_stability.resolve_tool_alias", return_value=("my_tool", None)):
            name, args, result_ctx = await resolve_alias("my_tool", {}, ctx)
            assert name == "my_tool"
            assert result_ctx.migration_note is None

    @pytest.mark.asyncio
    async def test_with_alias(self):
        ctx = DispatchContext()
        alias_info = MagicMock()
        alias_info.migration_note = "Use new_tool instead"
        alias_info.inject_action = "list"
        with patch("src.mcp_handlers.tool_stability.resolve_tool_alias", return_value=("new_tool", alias_info)):
            name, args, result_ctx = await resolve_alias("old_tool", {}, ctx)
            assert name == "new_tool"
            assert result_ctx.migration_note == "Use new_tool instead"
            assert args["action"] == "list"


class TestValidateParams:
    @pytest.mark.asyncio
    async def test_valid_params(self):
        """validate_params now uses Pydantic schemas directly."""
        ctx = DispatchContext()
        # With no Pydantic schema registered, params pass through unchanged
        with patch("src.tool_schemas.get_pydantic_schemas", return_value={}):
            result = await validate_params("unknown_tool", {"a": 1}, ctx)
            assert isinstance(result, tuple)
            assert result[1]["a"] == 1

    @pytest.mark.asyncio
    async def test_validation_error(self):
        """Pydantic validation error returns error list."""
        from pydantic import BaseModel
        class StrictModel(BaseModel):
            x: int
        ctx = DispatchContext()
        with patch("src.tool_schemas.get_pydantic_schemas", return_value={"strict_tool": StrictModel}):
            result = await validate_params("strict_tool", {"x": "not_int"}, ctx)
            assert isinstance(result, list)  # Error response

    @pytest.mark.asyncio
    async def test_enum_validation_error_suggests_common_alias(self):
        """Literal enum failures include valid values and an actionable suggestion."""
        from pydantic import BaseModel
        from typing import Literal

        class SeverityModel(BaseModel):
            severity: Literal["low", "medium", "high", "critical"]

        ctx = DispatchContext()
        with patch("src.tool_schemas.get_pydantic_schemas", return_value={"severity_tool": SeverityModel}):
            result = await validate_params("severity_tool", {"severity": "info"}, ctx)

        assert isinstance(result, list)
        data = json.loads(result[0].text)
        assert data["success"] is False
        assert data["parameter"] == "severity"
        assert data["suggested_value"] == "low"
        assert data["valid_values"] == ["low", "medium", "high", "critical"]
        assert "Did you mean 'low'" in data["error"]

    @pytest.mark.asyncio
    async def test_multiple_missing_params_returned_together(self):
        """Agents get all missing required fields in one validation response."""
        from pydantic import BaseModel

        class ThesisModel(BaseModel):
            root_cause: str
            proposed_conditions: list[str]

        ctx = DispatchContext()
        with patch("src.tool_schemas.get_pydantic_schemas", return_value={"thesis_tool": ThesisModel}):
            result = await validate_params("thesis_tool", {}, ctx)

        assert isinstance(result, list)
        data = json.loads(result[0].text)
        assert data["success"] is False
        assert data["missing_parameters"] == ["root_cause", "proposed_conditions"]
        assert "root_cause, proposed_conditions" in data["error"]

    @pytest.mark.asyncio
    async def test_passthrough_with_no_schema(self):
        """Without schema, params pass through with aliases applied."""
        ctx = DispatchContext()
        with patch("src.tool_schemas.get_pydantic_schemas", return_value={}):
            result = await validate_params("tool", {"x": "1"}, ctx)
            name, args, _ = result
            assert args["x"] == "1"


class TestCheckRateLimit:
    @pytest.mark.asyncio
    async def test_read_only_tool_skips(self):
        ctx = DispatchContext()
        name, args, _ = await check_rate_limit("health_check", {}, ctx)
        assert name == "health_check"

    @pytest.mark.asyncio
    async def test_rate_limit_allowed(self):
        ctx = DispatchContext()
        mock_limiter = MagicMock()
        mock_limiter.check_rate_limit.return_value = (True, None)
        with patch("src.mcp_handlers.middleware.rate_limit_step.get_rate_limiter", return_value=mock_limiter):
            result = await check_rate_limit("process_agent_update", {"agent_id": "a1"}, ctx)
            assert isinstance(result, tuple)

    @pytest.mark.asyncio
    async def test_rate_limit_denied(self):
        ctx = DispatchContext()
        mock_limiter = MagicMock()
        mock_limiter.check_rate_limit.return_value = (False, "Too many requests")
        mock_limiter.get_stats.return_value = {"calls": 100}
        with patch("src.mcp_handlers.middleware.rate_limit_step.get_rate_limiter", return_value=mock_limiter):
            result = await check_rate_limit("process_agent_update", {"agent_id": "a1"}, ctx)
            assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_loop_detection(self):
        ctx = DispatchContext()
        # Fill up history for list_agents
        history = _tool_call_history["list_agents"]
        history.clear()
        now = time.time()
        for i in range(25):
            history.append(now - 10 + i * 0.1)
        result = await check_rate_limit("list_agents", {}, ctx)
        assert isinstance(result, list)  # short-circuited
        # Clean up
        history.clear()
