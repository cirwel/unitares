"""Unit tests for the dialectic canary's pure evaluators (#1387 positive control).

The canary's value is that its checks encode the three historical false-zero
shapes; these tests pin each shape to the verdict it must produce.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
