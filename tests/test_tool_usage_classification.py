"""Tests for classify_tool_result — the discriminator that makes tool_usage.success
an EISV-blind external signal.

The hinge: only error_response() sets success=False (genuine validation/auth/state/
system errors). success_response() always sets success=True and spreads governance
verdicts (pause/reject) into the payload. A paused agent is a SUCCESSFUL tool call,
not a failure, and must never be logged as one — otherwise the external label becomes
circular (EISV drives the pause it would then be "validated" against).
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.tool_usage_recorder import classify_tool_result


def _text_result(payload: dict):
    """Mimic a single-element list of MCP TextContent (what dispatch_tool returns)."""
    return [SimpleNamespace(text=json.dumps(payload))]


# --- unit: classify_tool_result -------------------------------------------------

def test_error_payload_dict_is_failure():
    ok, etype = classify_tool_result(
        {"success": False, "error": "bad", "error_category": "validation_error"}
    )
    assert ok is False
    assert etype == "validation_error"


def test_error_payload_textcontent_is_failure():
    ok, etype = classify_tool_result(
        _text_result({"success": False, "error": "boom", "error_code": "STATE_001"})
    )
    assert ok is False
    assert etype == "STATE_001"


def test_error_payload_without_category_falls_back():
    ok, etype = classify_tool_result({"success": False, "error": "x"})
    assert ok is False
    assert etype == "tool_error"


def test_governance_pause_verdict_is_success():
    """CRITICAL: a pause verdict is success=True — must NOT be flagged as a failure."""
    ok, etype = classify_tool_result(
        _text_result({"success": True, "verdict": "pause", "phi": 0.05})
    )
    assert ok is True
    assert etype is None


def test_governance_reject_verdict_is_success():
    ok, etype = classify_tool_result(
        _text_result({"success": True, "verdict": "reject", "regime": "divergence"})
    )
    assert ok is True
    assert etype is None


def test_state_error_pause_gate_is_excluded():
    """CRITICAL (council): AGENT_PAUSED/ARCHIVED gate refusals carry error_category=
    state_error and are EISV-CAUSED — must be treated as no-signal, not a failure,
    or the label becomes circular (a paused agent's later calls fail only because
    EISV paused it)."""
    ok, etype = classify_tool_result(
        _text_result({"success": False, "error": "Agent is paused",
                      "error_code": "AGENT_PAUSED", "error_category": "state_error"})
    )
    assert ok is True
    assert etype is None


def test_multi_element_list_failure_in_element_zero_is_caught():
    """A failure payload in element 0 of a multi-element list must not slip through."""
    result = [SimpleNamespace(text=json.dumps(
        {"success": False, "error": "boom", "error_category": "system_error"})),
        SimpleNamespace(text="trailing chunk")]
    ok, etype = classify_tool_result(result)
    assert ok is False
    assert etype == "system_error"


def test_plain_success_payload():
    ok, etype = classify_tool_result({"success": True, "data": 1})
    assert ok == (True) and etype is None


def test_payload_without_success_key_is_success():
    # health_check-style data with no explicit success flag -> no failure signal
    ok, etype = classify_tool_result({"status": "healthy"})
    assert ok is True and etype is None


def test_unparseable_result_is_success():
    ok, etype = classify_tool_result([MagicMock()])  # .text is not JSON
    assert ok is True and etype is None
    assert classify_tool_result(None) == (True, None)
    assert classify_tool_result("not a payload") == (True, None)


# --- integration: the recorder actually receives the classified verdict ---------

def _consume_coro(coro, name=None):
    if hasattr(coro, "close"):
        coro.close()
    return MagicMock()


@pytest.mark.asyncio
async def test_http_direct_handler_error_payload_records_failure():
    from src.services.http_tool_service import execute_http_tool

    mock_tracker = MagicMock()
    err = _text_result({"success": False, "error": "nope", "error_category": "auth_error"})
    direct_handler = AsyncMock(return_value=err)

    with patch("src.services.http_tool_service.get_direct_http_tool_handler",
               return_value=direct_handler), \
         patch("src.tool_usage_tracker.get_tool_usage_tracker", return_value=mock_tracker), \
         patch("src.background_tasks.create_tracked_task", side_effect=_consume_coro):
        await execute_http_tool("identity", {"agent_id": "a1"})

    kwargs = mock_tracker.log_tool_call.call_args.kwargs
    assert kwargs["success"] is False
    assert kwargs["error_type"] == "auth_error"


@pytest.mark.asyncio
async def test_http_pause_verdict_records_success():
    """A governance pause flowing through the HTTP path is logged success=True."""
    from src.services.http_tool_service import execute_http_tool

    mock_tracker = MagicMock()
    paused = _text_result({"success": True, "verdict": "pause", "phi": 0.04})
    direct_handler = AsyncMock(return_value=paused)

    with patch("src.services.http_tool_service.get_direct_http_tool_handler",
               return_value=direct_handler), \
         patch("src.tool_usage_tracker.get_tool_usage_tracker", return_value=mock_tracker), \
         patch("src.background_tasks.create_tracked_task", side_effect=_consume_coro):
        await execute_http_tool("process_agent_update", {"agent_id": "a1"})

    kwargs = mock_tracker.log_tool_call.call_args.kwargs
    assert kwargs["success"] is True
    assert kwargs["error_type"] is None


def test_details_spread_error_type_is_used_when_category_and_code_absent():
    """Legacy error_response() refusals (reserved-prefix guard) carry only a
    details-spread error_type; the audit row must not collapse to tool_error."""
    payload = {"success": False, "error_type": "reserved_prefix",
               "error": "SECURITY: agent_id 'mcp_x' uses reserved prefix"}
    success, error_type = classify_tool_result(payload)
    assert success is False
    assert error_type == "reserved_prefix"


def test_error_category_still_wins_over_details_error_type():
    payload = {"success": False, "error_category": "identity_error",
               "error_type": "reserved_prefix"}
    success, error_type = classify_tool_result(payload)
    assert success is False
    assert error_type == "identity_error"
