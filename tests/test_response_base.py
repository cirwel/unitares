"""Regression tests for src/mcp_handlers/response_base.py.

Response-formatting helpers shared across handlers: the metrics report builder,
its text renderer (EISV / float formatting), and the success envelope (agent
signature, lite-response opt-out, param-coercion surfacing). Untested before;
these pin the output contract.
"""

from __future__ import annotations

import json

import pytest

from src.mcp_handlers.response_base import (
    format_metrics_report,
    format_metrics_text,
    success_response,
)


# --------------------------------------------------------------------------- #
# format_metrics_report
# --------------------------------------------------------------------------- #

class TestFormatMetricsReport:
    def test_includes_agent_id_and_timestamp(self):
        out = format_metrics_report({"coherence": 0.5}, agent_id="agent-1")
        assert out["agent_id"] == "agent-1"
        assert "timestamp" in out

    def test_timestamp_suppressed_when_disabled(self):
        out = format_metrics_report({"coherence": 0.5}, agent_id="a",
                                    include_timestamp=False)
        assert "timestamp" not in out

    def test_eisv_subdict_built_from_components(self):
        out = format_metrics_report(
            {"E": 0.1, "I": 0.8, "S": 0.2, "V": 0.0}, agent_id="a")
        assert out["eisv"] == {"E": 0.1, "I": 0.8, "S": 0.2, "V": 0.0}

    def test_no_eisv_key_when_no_components(self):
        out = format_metrics_report({"coherence": 0.5}, agent_id="a")
        assert "eisv" not in out

    def test_text_style_returns_string(self):
        out = format_metrics_report({"E": 0.1, "I": 0.8, "S": 0.2, "V": 0.0},
                                    agent_id="a", format_style="text")
        assert isinstance(out, str)
        assert "Agent: a" in out


# --------------------------------------------------------------------------- #
# format_metrics_text
# --------------------------------------------------------------------------- #

class TestFormatMetricsText:
    def test_agent_line_always_present(self):
        assert format_metrics_text({}).startswith("Agent: unknown")

    def test_eisv_from_subdict_three_decimals(self):
        text = format_metrics_text({"eisv": {"E": 0.12345, "I": 0.8, "S": 0.2, "V": 0.0}})
        assert "E=0.123" in text
        assert "I=0.800" in text

    def test_eisv_from_top_level_components(self):
        # The elif branch: no 'eisv' key but E/I/S/V at top level.
        text = format_metrics_text({"E": 0.5, "I": 0.6, "S": 0.7, "V": 0.8})
        assert "E=0.500 I=0.600 S=0.700 V=0.800" in text

    def test_float_metrics_rounded_strings_passthrough(self):
        text = format_metrics_text({"coherence": 0.55555, "verdict": "PROCEED"})
        assert "coherence: 0.556" in text   # float → 3 decimals
        assert "verdict: PROCEED" in text    # non-float → as-is


# --------------------------------------------------------------------------- #
# success_response
# --------------------------------------------------------------------------- #

class TestSuccessResponse:
    def _payload(self, *args, **kwargs):
        seq = success_response(*args, **kwargs)
        assert len(seq) == 1
        return json.loads(seq[0].text)

    def test_basic_success_envelope(self):
        d = self._payload({"value": 42}, agent_id="a")
        assert d["success"] is True
        assert d["value"] == 42
        assert "server_time" in d
        assert "agent_signature" in d

    def test_lite_response_omits_signature(self):
        d = self._payload({"value": 1}, agent_id="a",
                          arguments={"lite_response": True})
        assert "agent_signature" not in d

    def test_param_coercions_surfaced(self):
        d = self._payload(
            {"value": 1}, agent_id="a",
            arguments={"_param_coercions": {"complexity": "0.5->0.5"}})
        assert d["_param_coercions"]["applied"] == {"complexity": "0.5->0.5"}
        assert "note" in d["_param_coercions"]

    def test_param_coercions_hidden_in_lite(self):
        d = self._payload(
            {"value": 1}, agent_id="a",
            arguments={"lite_response": True, "_param_coercions": {"x": "y"}})
        assert "_param_coercions" not in d
