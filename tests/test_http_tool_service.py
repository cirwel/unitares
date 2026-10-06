from unittest.mock import AsyncMock, patch

import pytest
from mcp.types import TextContent

from src.services.http_tool_service import (
    _normalize_direct_http_result,
    execute_http_tool,
    get_direct_http_tool_handler,
)


class TestHttpToolService:

    @pytest.mark.asyncio
    async def test_core_tool_uses_direct_handler(self):
        direct_result = {"status": "healthy"}
        mock_handler = AsyncMock(return_value=direct_result)
        with patch.dict(
            "src.services.http_tool_service._DIRECT_HTTP_TOOL_HANDLERS",
            {"health_check": mock_handler},
            clear=False,
        ), patch(
            "src.services.http_tool_service.execute_http_dispatch_fallback",
            new=AsyncMock(),
        ) as mock_dispatch:
            result = await execute_http_tool("health_check", {})

        assert result == direct_result
        mock_handler.assert_awaited_once_with({})
        mock_dispatch.assert_not_called()

    @pytest.mark.legacy_identity_defaults
    @pytest.mark.asyncio
    async def test_non_core_tool_falls_back_to_dispatch(self):
        dispatch_result = [object()]
        with patch(
            "src.services.http_tool_service.execute_http_dispatch_fallback",
            new=AsyncMock(return_value=dispatch_result),
        ) as mock_dispatch:
            result = await execute_http_tool("some_other_tool", {"arg": "val"})

        assert result == dispatch_result
        mock_dispatch.assert_awaited_once_with("some_other_tool", {"arg": "val"})

    def test_known_direct_tool_is_registered(self):
        assert get_direct_http_tool_handler("identity") is not None
        assert get_direct_http_tool_handler("process_agent_update") is not None

    @pytest.mark.legacy_identity_defaults
    @pytest.mark.asyncio
    async def test_process_agent_update_uses_direct_handler(self):
        direct_result = [TextContent(type="text", text='{"success": true, "verdict": "proceed"}')]
        mock_handler = AsyncMock(return_value=direct_result)
        with patch.dict(
            "src.services.http_tool_service._DIRECT_HTTP_TOOL_HANDLERS",
            {"process_agent_update": mock_handler},
            clear=False,
        ), patch(
            "src.services.http_tool_service.execute_http_dispatch_fallback",
            new=AsyncMock(),
        ) as mock_dispatch:
            result = await execute_http_tool("process_agent_update", {"response_text": "done"})

        assert result == {"success": True, "verdict": "proceed"}
        mock_handler.assert_awaited_once_with({"response_text": "done"})
        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_identity_direct_handler_result_is_unwrapped_to_dict(self):
        mock_handler = AsyncMock(return_value=[
            TextContent(type="text", text='{"success": true, "uuid": "abc"}')
        ])
        with patch.dict(
            "src.services.http_tool_service._DIRECT_HTTP_TOOL_HANDLERS",
            {"identity": mock_handler},
            clear=False,
        ):
            result = await execute_http_tool("identity", {"client_session_id": "sess-1"})

        assert result == {"success": True, "uuid": "abc"}

    def test_normalize_direct_http_result_leaves_non_json_textcontent_untouched(self):
        raw_result = [TextContent(type="text", text="plain text")]
        assert _normalize_direct_http_result(raw_result) == raw_result
