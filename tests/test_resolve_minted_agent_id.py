"""Tests for resolve_minted_agent_id — audit attribution of identity-minting tools.

Onboard's audit.tool_usage rows carried agent_id=NULL because the UUID does
not exist until the handler returns (found 2026-06-12: onboard→first-checkin
conversion was unmeasurable from audit alone). The resolver back-fills the
audit attribution from the response payload for the minting tools only.
"""

import json

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.tool_usage_recorder import resolve_minted_agent_id


class _Text:
    def __init__(self, payload: dict):
        self.text = json.dumps(payload)


MINTED = "7750bf80-20ad-4108-a952-5271b73845b8"


def test_onboard_null_agent_falls_back_to_top_level_uuid():
    result = [_Text({"success": True, "uuid": MINTED})]
    assert resolve_minted_agent_id("onboard", None, result) == MINTED


def test_alias_envelope_falls_back_to_raw_governance_uuid():
    result = [_Text({"success": True, "raw_governance": {"uuid": MINTED}})]
    assert resolve_minted_agent_id("start_session", None, result) == MINTED


def test_agent_signature_uuid_is_last_resort():
    result = [_Text({"success": True, "agent_signature": {"uuid": MINTED}})]
    assert resolve_minted_agent_id("onboard", None, result) == MINTED


def test_request_side_identity_always_wins():
    result = [_Text({"success": True, "uuid": MINTED})]
    assert resolve_minted_agent_id("onboard", "explicit-caller", result) == "explicit-caller"


def test_non_minting_tool_is_never_back_filled():
    result = [_Text({"success": True, "uuid": MINTED, "agent_signature": {"uuid": MINTED}})]
    assert resolve_minted_agent_id("knowledge", None, result) is None


def test_unparseable_result_returns_input_unchanged():
    assert resolve_minted_agent_id("onboard", None, object()) is None
    assert resolve_minted_agent_id("onboard", None, [MagicMock(text="not json")]) is None


def test_payload_without_uuid_returns_none():
    result = [_Text({"success": True, "agent_id": "Structured_Handle_20260612"})]
    assert resolve_minted_agent_id("onboard", None, result) is None


def test_dict_result_is_supported():
    assert resolve_minted_agent_id("onboard", None, {"uuid": MINTED}) == MINTED


def _consume_coro(coro, name=None):
    if hasattr(coro, "close"):
        coro.close()
    return MagicMock()


@pytest.mark.asyncio
async def test_http_onboard_audit_row_carries_minted_uuid():
    """End-to-end through execute_http_tool: the JSONL tracker (same agent_id
    the DB write receives) gets the minted UUID, not None."""
    from src.services.http_tool_service import execute_http_tool

    mock_tracker = MagicMock()
    direct_handler = AsyncMock(return_value={"success": True, "uuid": MINTED})

    with patch("src.services.http_tool_service.get_direct_http_tool_handler",
               return_value=direct_handler), \
         patch("src.tool_usage_tracker.get_tool_usage_tracker", return_value=mock_tracker), \
         patch("src.background_tasks.create_tracked_task", side_effect=_consume_coro):
        await execute_http_tool("onboard", {"force_new": True})

    assert mock_tracker.log_tool_call.call_args.kwargs["agent_id"] == MINTED
