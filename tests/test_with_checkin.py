from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/dev/with_checkin.py"
SPEC = importlib.util.spec_from_file_location("with_checkin", MODULE_PATH)
with_checkin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = with_checkin
assert SPEC.loader is not None
SPEC.loader.exec_module(with_checkin)


def test_infer_workflow_for_common_commands():
    assert with_checkin.infer_workflow(["python3", "-m", "pytest", "tests"]) == "test"
    assert with_checkin.infer_workflow(["pytest", "tests"]) == "test"
    assert with_checkin.infer_workflow(["./scripts/dev/test-cache.sh"]) == "test"
    assert with_checkin.infer_workflow(["scripts/dev/test-cache.sh", "--staged"]) == "test"
    assert with_checkin.infer_workflow(["make", "smoke"]) == "test"
    assert with_checkin.infer_workflow(["mix", "test"]) == "test"
    assert with_checkin.infer_workflow(["git", "commit", "-m", "msg"]) == "commit"
    assert with_checkin.infer_workflow(["git", "push", "origin", "HEAD"]) == "push"
    assert (
        with_checkin.infer_workflow(
            ["python3", "scripts/diagnostics/r2_phase1_telemetry.py"]
        )
        == "diagnostic"
    )
    assert with_checkin.infer_workflow(["python3", "-c", "print('ok')"]) == "command"


def test_build_checkin_payload_records_successful_s22_evidence():
    result = with_checkin.CommandResult(
        argv=["python3", "-m", "pytest", "tests/test_core.py"],
        exit_code=0,
        duration_sec=1.234,
        output_tail=["1 passed"],
    )
    context = with_checkin.CheckinContext(
        workflow="test",
        client_session_id="sid-123",
        comparison_key="pair-key",
        task_label="Run unit tests",
        invocation_id="inv-1",
        tool_surface=["terminal", "mcp:unitares"],
    )

    payload = with_checkin.build_checkin_payload(result, context)

    from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams

    ProcessAgentUpdateParams.model_validate(payload)
    assert payload["response_mode"] == "compact"
    assert payload["client_session_id"] == "sid-123"
    assert "continuity_token" not in payload
    assert payload["task_type"] == "testing"
    assert payload["confidence"] == 0.82
    assert payload["harness_type"] == "codex-cli"
    assert payload["model_provider"] == "openai"
    assert payload["model"] == "gpt-5.5"
    assert payload["transport"] == "terminal"
    assert payload["tool_surface"] == ["terminal", "mcp:unitares"]
    assert payload["governance_mode"] == "explicit"
    assert payload["verification_source"] == "agent_reported_tool_result"
    assert payload["comparison_key"] == "pair-key"
    assert payload["task_label"] == "Run unit tests"
    assert payload["task_outcome"] == "succeeded"
    assert payload["invocation_id"] == "inv-1"
    assert "with_checkin ran test workflow" in payload["response_text"]
    evidence = payload["recent_tool_results"][0]
    assert evidence["kind"] == "test"
    assert evidence["tool"] == "python3 -m pytest"
    assert evidence["exit_code"] == 0
    assert evidence["is_bad"] is False
    assert len(evidence["summary"]) <= 512


def test_build_checkin_payload_records_failed_command_without_overlong_summary():
    result = with_checkin.CommandResult(
        argv=["tool", "x" * 800],
        exit_code=2,
        duration_sec=2.5,
        output_tail=["failure"],
    )
    context = with_checkin.CheckinContext(workflow="command", comparison_key="pair-key")

    payload = with_checkin.build_checkin_payload(result, context)

    assert payload["task_type"] == "mixed"
    assert payload["confidence"] == 0.42
    assert payload["task_outcome"] == "failed"
    evidence = payload["recent_tool_results"][0]
    assert evidence["kind"] == "command"
    assert evidence["exit_code"] == 2
    assert evidence["is_bad"] is True
    assert len(evidence["summary"]) == 512


def test_run_command_streams_output_and_preserves_exit_code(capsys):
    result = with_checkin.run_command(
        [sys.executable, "-c", "print('ok')"],
        max_tail_lines=5,
    )

    captured = capsys.readouterr()
    assert result.exit_code == 0
    assert result.output_tail == ["ok"]
    assert "ok" in captured.out


def test_call_process_agent_update_posts_mcp_tool_shape(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"success": true, "verdict": "proceed"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["session"] = req.get_header("X-session-id")
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(with_checkin.urllib.request, "urlopen", fake_urlopen)

    result = with_checkin.call_process_agent_update(
        "http://unit.test/",
        {"response_text": "hello"},
        session_id="session-1",
        timeout=7,
    )

    assert result == {"success": True, "verdict": "proceed"}
    assert captured["url"] == "http://unit.test/v1/tools/call"
    assert captured["session"] == "session-1"
    assert captured["body"] == {
        "name": "process_agent_update",
        "arguments": {"response_text": "hello"},
    }
    assert captured["timeout"] == 7


def test_extract_verdict_from_text_content():
    result = {
        "content": [
            {
                "type": "text",
                "text": '{"success": true, "verdict": "guide"}',
            }
        ]
    }

    assert with_checkin.extract_verdict(result) == "guide"
