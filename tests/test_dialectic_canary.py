"""Unit tests for the dialectic canary's evaluators and terminal poller.

The canary's value is that its checks encode the three historical false-zero
shapes and prove that a reviewer spawn is not reported green before the review
records a terminal verdict.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

CANARY_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "ops" / "dialectic_canary.py"
)
spec = importlib.util.spec_from_file_location("dialectic_canary", CANARY_PATH)
canary = importlib.util.module_from_spec(spec)
sys.modules["dialectic_canary"] = canary
spec.loader.exec_module(canary)


class TestEvaluateOnboard:
    def test_happy_path(self):
        ok, _ = canary.evaluate_onboard({
            "success": True,
            "agent_uuid": "u-1",
            "raw_governance": {"uuid": "u-1", "display_name": "canary_dialectic"},
        })
        assert ok

    def test_label_without_prefix_refuses(self):
        """If the server composes the label differently the KPI exclusion
        silently stops matching — the canary must fail LOUDLY, not pollute."""
        ok, detail = canary.evaluate_onboard({
            "success": True,
            "agent_uuid": "u-1",
            "raw_governance": {"uuid": "u-1", "display_name": "claude_code-canary_dialectic"},
        })
        assert not ok
        assert "prefix" in detail

    def test_failed_onboard(self):
        ok, _ = canary.evaluate_onboard({"success": False, "error": "boom"})
        assert not ok

    def test_missing_uuid(self):
        ok, detail = canary.evaluate_onboard({"success": True, "raw_governance": {}})
        assert not ok
        assert "uuid" in detail


class TestEvaluateReview:
    def _base(self, **over):
        payload = {
            "success": True,
            "session_id": "sess-1",
            "one_call_review": True,
            "thesis_recorded": True,
        }
        payload.update(over)
        return payload

    def test_happy_path(self):
        ok, _ = canary.evaluate_review(self._base())
        assert ok

    def test_enveloped_payload(self):
        """Live shape: the alias layer nests the handler JSON under
        raw_governance; the top level has only convenience fields. The first
        live canary run failed exactly here — pin it."""
        ok, _ = canary.evaluate_review({
            "success": True,
            "tool": "request_review",
            "state_summary": {"session_id": "sess-1"},
            "raw_governance": self._base(),
        })
        assert ok

    def test_enveloped_failure_is_red(self):
        ok, detail = canary.evaluate_review({
            "success": True,
            "raw_governance": {"success": False, "error": "boom", "session_id": None},
        })
        assert not ok

    def test_timeout_shape_is_red(self):
        """#1442: the decorator timeout returns success=False with an error."""
        ok, detail = canary.evaluate_review({
            "success": False,
            "error": "Tool 'request_dialectic_review' timed out after 60.0 seconds.",
        })
        assert not ok
        assert "timed out" in detail

    def test_thesis_not_recorded_is_red(self):
        """#1414: session row committed, thesis discarded."""
        ok, detail = canary.evaluate_review(self._base(thesis_recorded=False))
        assert not ok
        assert "NOT recorded" in detail

    def test_missing_session_id_is_red(self):
        ok, _ = canary.evaluate_review(self._base(session_id=None))
        assert not ok

    def test_two_call_fallthrough_is_red(self):
        """A response that never entered the one-call branch means the probe
        did not exercise the surface the gate measures."""
        ok, detail = canary.evaluate_review(self._base(one_call_review=None))
        assert not ok
        assert "one_call_review" in detail


class TestEvaluateTerminalReview:
    def test_resolved_with_action_is_green(self):
        terminal, ok, detail = canary.evaluate_terminal_review({
            "success": True,
            "session_id": "sess-1",
            "phase": "resolved",
            "resolution": {"action": "resume"},
        })
        assert terminal
        assert ok
        assert detail == "ok"

    def test_enveloped_resolved_payload_is_green(self):
        terminal, ok, _ = canary.evaluate_terminal_review({
            "success": True,
            "raw_governance": {
                "success": True,
                "session_id": "sess-1",
                "phase": "resolved",
                "resolution": {"action": "revise"},
            },
        })
        assert terminal
        assert ok

    def test_non_terminal_phase_keeps_polling(self):
        terminal, ok, detail = canary.evaluate_terminal_review({
            "success": True,
            "session_id": "sess-1",
            "phase": "synthesis",
        })
        assert not terminal
        assert not ok
        assert "synthesis" in detail

    @pytest.mark.parametrize("phase", ["failed", "timeout", "abandoned", "escalated"])
    def test_non_resolved_terminal_phase_is_red(self, phase):
        terminal, ok, detail = canary.evaluate_terminal_review({
            "success": True,
            "session_id": "sess-1",
            "phase": phase,
        })
        assert terminal
        assert not ok
        assert phase in detail

    def test_resolved_without_action_is_red(self):
        terminal, ok, detail = canary.evaluate_terminal_review({
            "success": True,
            "session_id": "sess-1",
            "phase": "resolved",
            "resolution": {},
        })
        assert terminal
        assert not ok
        assert "no resolution action" in detail

    def test_get_error_is_terminal_red(self):
        terminal, ok, detail = canary.evaluate_terminal_review({
            "success": False,
            "error": "db down",
        })
        assert terminal
        assert not ok
        assert "db down" in detail


@pytest.mark.asyncio
async def test_wait_for_terminal_review_polls_until_resolved(monkeypatch):
    responses = [
        {"success": True, "session_id": "sess-1", "phase": "synthesis"},
        {
            "success": True,
            "session_id": "sess-1",
            "phase": "resolved",
            "resolution": {"action": "resume"},
        },
    ]
    calls = []

    async def fake_call_tool(url, tool_name, arguments, timeout_s):
        calls.append((url, tool_name, arguments, timeout_s))
        return responses.pop(0)

    monkeypatch.setattr(canary, "call_tool", fake_call_tool)
    payload, ok, detail, polls = await canary.wait_for_terminal_review(
        url="http://example.test/mcp/",
        session_id="sess-1",
        client_session_id="agent-csid",
        initial_payload={
            "success": True,
            "session_id": "sess-1",
            "phase": "antithesis",
        },
        timeout_s=10.0,
        poll_interval_s=0.0,
    )

    assert ok
    assert detail == "ok"
    assert polls == 2
    assert payload["phase"] == "resolved"
    assert [call[1] for call in calls] == ["dialectic", "dialectic"]
    assert calls[0][2] == {
        "action": "get",
        "session_id": "sess-1",
        "client_session_id": "agent-csid",
    }


@pytest.mark.asyncio
async def test_wait_for_terminal_review_times_out_without_polling():
    payload, ok, detail, polls = await canary.wait_for_terminal_review(
        url="http://example.test/mcp/",
        session_id="sess-1",
        client_session_id="agent-csid",
        initial_payload={
            "success": True,
            "session_id": "sess-1",
            "phase": "antithesis",
        },
        timeout_s=0.0,
        poll_interval_s=0.0,
    )

    assert not ok
    assert "did not reach" in detail
    assert polls == 0
    assert payload["phase"] == "antithesis"


def test_positive_float_env_rejects_invalid_and_nonpositive(monkeypatch):
    monkeypatch.setenv("UNITARES_CANARY_VERDICT_TIMEOUT_S", "not-a-number")
    assert canary._verdict_timeout_s() == 120.0
    monkeypatch.setenv("UNITARES_CANARY_VERDICT_TIMEOUT_S", "0")
    assert canary._verdict_timeout_s() == 120.0
    monkeypatch.setenv("UNITARES_CANARY_VERDICT_TIMEOUT_S", "45")
    assert canary._verdict_timeout_s() == 45.0
