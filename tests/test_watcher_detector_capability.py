"""Tests for Watcher's detector-capability escalation.

Regression coverage for the 2026-06-29 -> 07-29 silent outage: PR #1276 pointed
the default detector at an ollama tag that was never pulled, so every scan died
with HTTP 404 and produced no findings. "No findings" is also what a healthy
scan of clean code returns, so nothing anywhere changed state for 31 days —
420 errors accumulated in a log with no reader.

The fix under test: consecutive model-call failures escalate once, through the
one channel that reaches a human (a governance finding), with the failure class
carried in the fingerprint so a misconfiguration announces itself once rather
than once per edit.
"""

from __future__ import annotations

import json

import pytest

import agents.watcher.agent as agent


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Point the failure counter at a temp dir; never touch real state."""
    monkeypatch.setattr(agent, "watcher_state_dir", lambda: tmp_path)
    monkeypatch.setattr(agent, "MODEL_FAILURE_ESCALATE_AFTER", 3)
    yield


@pytest.fixture
def posted(monkeypatch):
    """Capture escalations instead of posting them."""
    calls: list[dict] = []
    monkeypatch.setattr(agent, "get_watcher_identity", lambda: {"agent_uuid": "uuid-1"})
    monkeypatch.setattr(agent, "post_finding", lambda **kw: calls.append(kw) or True)
    return calls


class TestClassification:
    @pytest.mark.parametrize(
        "exc, expected",
        [
            (Exception("HTTP Error 404: Not Found"), "model_not_found"),
            (Exception("model 'qwen3.6:27b-coding-nvfp4' not found"), "model_not_found"),
            (TimeoutError("timed out"), "timeout"),
            (Exception("Connection refused"), "unreachable"),
            (Exception("something else entirely"), "error"),
        ],
    )
    def test_failure_classes(self, exc, expected):
        # The class drives both the operator hint and the dedup fingerprint —
        # an unpulled tag and an over-budget model are different problems.
        assert agent._classify_model_failure(exc) == expected


class TestEscalation:
    def test_silent_below_threshold(self, posted):
        for _ in range(2):
            agent._record_model_failure(Exception("HTTP Error 404: Not Found"))
        assert posted == []

    def test_escalates_once_at_threshold(self, posted):
        for _ in range(6):
            agent._record_model_failure(Exception("HTTP Error 404: Not Found"))
        assert len(posted) == 1, "a persistent fault must announce itself once, not per scan"
        call = posted[0]
        assert call["event_type"] == "watcher_capability_finding"
        assert call["severity"] == "high"
        assert call["fingerprint"].startswith("watcher-capability:model_not_found:")
        assert call["extra"]["consecutive_failures"] == 3

    def test_a_different_failure_class_escalates_separately(self, posted):
        for _ in range(3):
            agent._record_model_failure(Exception("HTTP Error 404: Not Found"))
        for _ in range(3):
            agent._record_model_failure(TimeoutError("timed out"))
        classes = [c["extra"]["failure_class"] for c in posted]
        assert classes == ["model_not_found", "timeout"]

    def test_success_resets_the_counter(self, posted):
        for _ in range(2):
            agent._record_model_failure(Exception("HTTP Error 404: Not Found"))
        agent._clear_model_failures()
        agent._record_model_failure(Exception("HTTP Error 404: Not Found"))
        assert posted == [], "a recovered detector must not carry old failures toward escalation"

    def test_no_identity_does_not_raise(self, monkeypatch, tmp_path):
        # A detector that cannot report its own death must still not crash the
        # scan that discovered it.
        monkeypatch.setattr(agent, "get_watcher_identity", lambda: None)
        for _ in range(3):
            agent._record_model_failure(Exception("HTTP Error 404: Not Found"))
        state = json.loads((tmp_path / "model_failures.json").read_text())
        assert state["escalated"] is False
        assert state["count"] == 3

    def test_post_finding_raising_does_not_break_the_scan(self, monkeypatch, tmp_path):
        monkeypatch.setattr(agent, "get_watcher_identity", lambda: {"agent_uuid": "uuid-1"})

        def _boom(**_kw):
            raise RuntimeError("governance unreachable")

        monkeypatch.setattr(agent, "post_finding", _boom)
        for _ in range(3):
            agent._record_model_failure(Exception("HTTP Error 404: Not Found"))
        state = json.loads((tmp_path / "model_failures.json").read_text())
        assert state["escalated"] is False

    def test_state_survives_corrupt_file(self, posted, tmp_path):
        (tmp_path / "model_failures.json").write_text("{not json")
        for _ in range(3):
            agent._record_model_failure(Exception("HTTP Error 404: Not Found"))
        assert len(posted) == 1
