"""
Tests for src/mcp_handlers/decorators.py - MCP tool decorator and registry.

Tests the decorator mechanics, registration, and metadata queries.
"""

import pytest
import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.decorators import (
    mcp_tool,
    get_tool_registry,
    get_tool_timeout,
    get_tool_description,
    get_tool_metadata,
    is_tool_deprecated,
    is_tool_hidden,
    list_registered_tools,
    _TOOL_DEFINITIONS,
)


@pytest.fixture(autouse=True)
def clean_registry():
    """Clean up registry before/after each test to prevent cross-contamination."""
    orig = dict(_TOOL_DEFINITIONS)
    yield
    _TOOL_DEFINITIONS.clear()
    _TOOL_DEFINITIONS.update(orig)


class TestMcpToolDecorator:

    def test_registers_tool(self):
        @mcp_tool("test_tool_alpha")
        async def handle_test_tool_alpha(arguments):
            return []

        assert "test_tool_alpha" in _TOOL_DEFINITIONS

    def test_auto_name_from_function(self):
        @mcp_tool()
        async def handle_my_auto_tool(arguments):
            return []

        assert "my_auto_tool" in _TOOL_DEFINITIONS

    def test_custom_timeout(self):
        @mcp_tool("test_timeout_tool", timeout=120.0)
        async def handle_test_timeout_tool(arguments):
            return []

        assert get_tool_timeout("test_timeout_tool") == 120.0

    def test_default_timeout(self):
        @mcp_tool("test_default_to")
        async def handle_test_default_to(arguments):
            return []

        assert get_tool_timeout("test_default_to") == 30.0

    def test_unknown_tool_default_timeout(self):
        assert get_tool_timeout("nonexistent_tool") == 30.0

    def test_custom_description(self):
        @mcp_tool("test_desc_tool", description="A custom description")
        async def handle_test_desc_tool(arguments):
            return []

        assert get_tool_description("test_desc_tool") == "A custom description"

    def test_description_from_docstring(self):
        @mcp_tool("test_doc_tool")
        async def handle_test_doc_tool(arguments):
            """First line of docstring"""
            return []

        assert get_tool_description("test_doc_tool") == "First line of docstring"

    def test_register_false_not_in_registry(self):
        @mcp_tool("test_internal", register=False)
        async def handle_test_internal(arguments):
            return []

        assert "test_internal" not in _TOOL_DEFINITIONS

    def test_deprecated_metadata(self):
        @mcp_tool("test_old_tool", deprecated=True, superseded_by="test_new_tool")
        async def handle_test_old_tool(arguments):
            return []

        assert is_tool_deprecated("test_old_tool") is True
        meta = get_tool_metadata("test_old_tool")
        assert meta["superseded_by"] == "test_new_tool"

    def test_hidden_metadata(self):
        @mcp_tool("test_hidden_tool", hidden=True)
        async def handle_test_hidden_tool(arguments):
            return []

        assert is_tool_hidden("test_hidden_tool") is True

    def test_not_deprecated_by_default(self):
        @mcp_tool("test_normal_tool")
        async def handle_test_normal_tool(arguments):
            return []

        assert is_tool_deprecated("test_normal_tool") is False
        assert is_tool_hidden("test_normal_tool") is False


class TestListRegisteredTools:

    def test_list_includes_registered(self):
        @mcp_tool("test_list_a")
        async def handle_a(arguments):
            return []

        @mcp_tool("test_list_b")
        async def handle_b(arguments):
            return []

        tools = list_registered_tools()
        assert "test_list_a" in tools
        assert "test_list_b" in tools

    def test_list_excludes_hidden(self):
        @mcp_tool("test_visible")
        async def handle_vis(arguments):
            return []

        @mcp_tool("test_hidden_list", hidden=True)
        async def handle_hid(arguments):
            return []

        tools = list_registered_tools(include_hidden=False)
        assert "test_visible" in tools
        assert "test_hidden_list" not in tools

    def test_list_includes_hidden_when_requested(self):
        @mcp_tool("test_hidden_inc", hidden=True)
        async def handle_hid_inc(arguments):
            return []

        tools = list_registered_tools(include_hidden=True)
        assert "test_hidden_inc" in tools

    def test_list_excludes_deprecated_when_requested(self):
        @mcp_tool("test_dep_exc", deprecated=True)
        async def handle_dep(arguments):
            return []

        tools = list_registered_tools(include_deprecated=False)
        assert "test_dep_exc" not in tools

    def test_list_sorted(self):
        @mcp_tool("test_zzz")
        async def handle_z(arguments):
            return []

        @mcp_tool("test_aaa")
        async def handle_a(arguments):
            return []

        tools = list_registered_tools()
        aaa_idx = tools.index("test_aaa")
        zzz_idx = tools.index("test_zzz")
        assert aaa_idx < zzz_idx


class TestDecoratorExecution:

    @pytest.mark.asyncio
    async def test_wrapped_function_executes(self):
        @mcp_tool("test_exec", timeout=5.0)
        async def handle_test_exec(arguments):
            return [{"result": arguments.get("x", 0) + 1}]

        result = await handle_test_exec({"x": 41})
        assert result == [{"result": 42}]

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        @mcp_tool("test_slow", timeout=0.1)
        async def handle_test_slow(arguments):
            await asyncio.sleep(5)
            return []

        result = await handle_test_slow({})
        # Should return error response, not raise
        assert len(result) == 1
        # error_response returns TextContent with JSON
        text = result[0].text if hasattr(result[0], 'text') else str(result[0])
        assert "timed out" in text.lower()

    @pytest.mark.asyncio
    async def test_timeout_emits_coordination_failure_event(self):
        """Wave 0 step 2A: when @mcp_tool wrapper hits TimeoutError, it MUST
        call emit_coordination_failure_sync with the tool_decorator sub-type
        before returning the error_response. Verifies the wire — emit fires
        with correct event_type, payload, and agent_id extracted from arguments."""
        from unittest.mock import patch

        @mcp_tool("test_emit_on_timeout", timeout=0.05)
        async def handle_test_emit_on_timeout(arguments):
            await asyncio.sleep(5)
            return []

        with patch("src.coordination_failure_emit.emit_coordination_failure_sync") as mock_emit:
            result = await handle_test_emit_on_timeout({"agent_id": "test-uuid-1234"})

        # Wrapper still returns the error response — emit doesn't change behavior
        assert len(result) == 1
        text = result[0].text if hasattr(result[0], 'text') else str(result[0])
        assert "timed out" in text.lower()

        # Emit was called with the correct envelope
        mock_emit.assert_called_once()
        call_kwargs = mock_emit.call_args.kwargs
        assert call_kwargs["service"] == "governance_mcp"
        assert call_kwargs["event_type"] == "coordination_failure.mcp_handler_timeout.tool_decorator"
        assert call_kwargs["payload"]["tool_name"] == "test_emit_on_timeout"
        assert call_kwargs["payload"]["timeout_s"] == 0.05
        assert "elapsed_s" in call_kwargs["payload"]
        assert call_kwargs["agent_id"] == "test-uuid-1234"

    @pytest.mark.asyncio
    async def test_timeout_emit_handles_arguments_without_agent_id(self):
        """When arguments dict has no agent_id, emit passes None — column is nullable."""
        from unittest.mock import patch

        @mcp_tool("test_no_agent_id_on_timeout", timeout=0.05)
        async def handle_test_no_agent_id(arguments):
            await asyncio.sleep(5)
            return []

        with patch("src.coordination_failure_emit.emit_coordination_failure_sync") as mock_emit:
            await handle_test_no_agent_id({})

        mock_emit.assert_called_once()
        assert mock_emit.call_args.kwargs["agent_id"] is None


class TestGetToolMetadata:

    def test_unknown_tool_returns_empty(self):
        meta = get_tool_metadata("totally_nonexistent")
        assert meta == {}

    def test_unknown_tool_not_deprecated(self):
        assert is_tool_deprecated("totally_nonexistent") is False

    def test_unknown_tool_not_hidden(self):
        assert is_tool_hidden("totally_nonexistent") is False

    def test_unknown_tool_no_description(self):
        assert get_tool_description("totally_nonexistent") == ""
