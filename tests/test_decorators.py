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
    get_tool_identity_requirement,
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

    def test_leave_note_marked_deprecated(self):
        """leave_note is the issue-#429 canonical deprecation: superseded by knowledge(action='note')."""
        # Import handler to trigger decorator registration in the live registry.
        import src.mcp_handlers.knowledge.handlers  # noqa: F401

        assert is_tool_deprecated("leave_note") is True
        meta = get_tool_metadata("leave_note")
        assert meta["superseded_by"] == "knowledge"

    def test_describe_tool_surfaces_deprecation(self):
        """describe_tool must inject deprecation block for deprecated tools (#429 council fix).

        Pre-fix, the deprecation lived only in tool_relationships (consumed by
        list_tools) and on the decorator. describe_tool built responses from
        get_tool_definitions only — agents calling describe_tool('leave_note')
        got no migration hint.
        """
        from src.mcp_handlers.introspection.tool_introspection import (
            _describe_tool_deprecation_block,
        )
        block = _describe_tool_deprecation_block("leave_note")
        assert block is not None
        assert block["deprecated"] is True
        assert block["superseded_by"] == "knowledge"
        assert "knowledge" in block["migration"]
        assert "action='note'" in block["migration"]

        # Non-deprecated tool returns None
        assert _describe_tool_deprecation_block("process_agent_update") is None


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
        # Wave 0 step 2 dedup contract: every coordination_failure event carries
        # an incident_id UUID for §129 dedup. The decorator emit site was the
 # one gap .
        import uuid as _uuid
        incident_id = call_kwargs["payload"]["incident_id"]
        assert isinstance(incident_id, str)
        _uuid.UUID(incident_id)  # raises if not a valid UUID

    @pytest.mark.asyncio
    async def test_timeout_emit_handles_arguments_without_agent_id(self):
        """When arguments dict has no agent_id and no session context, emit
        passes None — column is nullable."""
        from unittest.mock import patch

        @mcp_tool("test_no_agent_id_on_timeout", timeout=0.05)
        async def handle_test_no_agent_id(arguments):
            await asyncio.sleep(5)
            return []

        with patch("src.coordination_failure_emit.emit_coordination_failure_sync") as mock_emit:
            await handle_test_no_agent_id({})

        mock_emit.assert_called_once()
        assert mock_emit.call_args.kwargs["agent_id"] is None
        assert mock_emit.call_args.kwargs["session_id"] is None

    @pytest.mark.asyncio
    async def test_timeout_emit_falls_back_to_context_agent_id(self):
        """For consolidated tools like observe(action=aggregate) that carry no
        agent_id arg, the decorator MUST fall back to the session contextvar.
        Without this, ~100% of timeouts attribute to NULL agent_id (observed
        empirically across 6 events post-2A merge)."""
        from unittest.mock import patch
        from src.mcp_handlers.context import set_session_context, reset_session_context

        @mcp_tool("test_ctx_fallback", timeout=0.05)
        async def handle_test_ctx_fallback(arguments):
            await asyncio.sleep(5)
            return []

        token = set_session_context(
            session_key="caller-session-key",
            client_session_id="client-1",
            agent_id="caller-uuid-from-ctx",
        )
        try:
            with patch("src.coordination_failure_emit.emit_coordination_failure_sync") as mock_emit:
                await handle_test_ctx_fallback({})  # no agent_id in args
        finally:
            reset_session_context(token)

        mock_emit.assert_called_once()
        assert mock_emit.call_args.kwargs["agent_id"] == "caller-uuid-from-ctx"
        assert mock_emit.call_args.kwargs["session_id"] == "caller-session-key"

    @pytest.mark.asyncio
    async def test_timeout_emit_arguments_agent_id_wins_over_context(self):
        """When arguments dict supplies an explicit agent_id, it takes precedence
        over the contextvar — the explicit target trumps the bound caller."""
        from unittest.mock import patch
        from src.mcp_handlers.context import set_session_context, reset_session_context

        @mcp_tool("test_args_wins", timeout=0.05)
        async def handle_test_args_wins(arguments):
            await asyncio.sleep(5)
            return []

        token = set_session_context(
            session_key="caller-session",
            client_session_id="client-1",
            agent_id="caller-uuid",
        )
        try:
            with patch("src.coordination_failure_emit.emit_coordination_failure_sync") as mock_emit:
                await handle_test_args_wins({"agent_id": "explicit-target-uuid"})
        finally:
            reset_session_context(token)

        mock_emit.assert_called_once()
        assert mock_emit.call_args.kwargs["agent_id"] == "explicit-target-uuid"
        # session_id still comes from context regardless
        assert mock_emit.call_args.kwargs["session_id"] == "caller-session"

    @pytest.mark.asyncio
    async def test_timeout_emit_surfaces_action_in_payload(self):
        """Consolidated tools dispatch via arguments['action']; surfacing it in
        payload lets downstream filters distinguish observe(action=aggregate)
        timeouts from observe(action=agent) timeouts without payload archaeology."""
        from unittest.mock import patch

        @mcp_tool("test_action_payload", timeout=0.05)
        async def handle_test_action_payload(arguments):
            await asyncio.sleep(5)
            return []

        with patch("src.coordination_failure_emit.emit_coordination_failure_sync") as mock_emit:
            await handle_test_action_payload({"action": "aggregate"})

        mock_emit.assert_called_once()
        assert mock_emit.call_args.kwargs["payload"]["action"] == "aggregate"

    @pytest.mark.asyncio
    async def test_timeout_emit_omits_action_key_when_absent(self):
        """Non-consolidated tools (no 'action' arg) don't leak a phantom action
        key — payload stays minimal."""
        from unittest.mock import patch

        @mcp_tool("test_no_action_key", timeout=0.05)
        async def handle_test_no_action_key(arguments):
            await asyncio.sleep(5)
            return []

        with patch("src.coordination_failure_emit.emit_coordination_failure_sync") as mock_emit:
            await handle_test_no_action_key({"some_other_arg": "x"})

        mock_emit.assert_called_once()
        assert "action" not in mock_emit.call_args.kwargs["payload"]


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


class TestRequiresIdentity:
    """The requires_identity attribute (#425) declares per-tool identity-bootstrap
    behavior. Default is 'required'; pre_onboard tools may run without a bound
    identity. The attribute is the source of truth that the dispatch middleware
    consumes."""

    def test_default_is_required(self):
        @mcp_tool("test_default_identity_tool")
        async def handle_test_default_identity_tool(arguments):
            return []

        assert _TOOL_DEFINITIONS["test_default_identity_tool"].requires_identity == "required"
        assert get_tool_identity_requirement("test_default_identity_tool") == "required"

    def test_explicit_pre_onboard(self):
        @mcp_tool("test_pre_onboard_tool", requires_identity="pre_onboard")
        async def handle_test_pre_onboard_tool(arguments):
            return []

        assert get_tool_identity_requirement("test_pre_onboard_tool") == "pre_onboard"

    def test_explicit_scoped(self):
        @mcp_tool("test_scoped_tool", requires_identity="scoped")
        async def handle_test_scoped_tool(arguments):
            return []

        assert get_tool_identity_requirement("test_scoped_tool") == "scoped"

    def test_invalid_requires_identity_raises(self):
        with pytest.raises(ValueError, match="requires_identity must be one of"):
            @mcp_tool("test_bad_value", requires_identity="anonymous")
            async def handle_test_bad_value(arguments):
                return []

    def test_unknown_tool_defaults_to_required(self):
        # Fail-closed: tools not in the registry must be treated as
        # identity-required by middleware (otherwise an unregistered handler
        # could surface as pre-onboard exempt).
        assert get_tool_identity_requirement("totally_nonexistent_tool") == "required"

    def test_function_attribute_attached(self):
        @mcp_tool("test_attr_attached", requires_identity="pre_onboard")
        async def handle_test_attr_attached(arguments):
            return []

        assert handle_test_attr_attached._mcp_requires_identity == "pre_onboard"


class TestPreOnboardToolsClassification:
    """Verify the canonical PRE_ONBOARD set is correctly declared on the
    handlers themselves (not just the test). These tools must run pre-onboard
    or the agent cannot inspect the protocol surface to decide what to call."""

    EXPECTED_PRE_ONBOARD = {
        "health_check",
        "list_tools",
        "describe_tool",
        "get_governance_metrics",
        "skills",
        "identity",
        "onboard",
        "bind_session",
    }

    def test_all_canonical_pre_onboard_tools_are_declared(self):
        # Trigger handler registration by importing the modules.
        import src.mcp_handlers  # noqa: F401

        for name in self.EXPECTED_PRE_ONBOARD:
            actual = get_tool_identity_requirement(name)
            assert actual == "pre_onboard", (
                f"Tool {name!r} should be requires_identity='pre_onboard' but is {actual!r}. "
                f"Either fix the handler decorator or update this test if the canonical set changed."
            )

    def test_write_tools_are_required(self):
        import src.mcp_handlers  # noqa: F401

        # Sample of write-bearing tools that must require identity.
        for name in ("process_agent_update", "leave_note", "knowledge"):
            actual = get_tool_identity_requirement(name)
            assert actual == "required", (
                f"Write-bearing tool {name!r} must be requires_identity='required' "
                f"to gate at the middleware boundary; got {actual!r}."
            )
