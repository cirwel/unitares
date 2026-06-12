"""
Tests for the dispatch pipeline middleware steps.

Tests individual middleware functions from src/mcp_handlers/middleware.py
and the dispatch_tool integration from src/mcp_handlers/__init__.py.

Middleware signature: async (name, arguments, ctx) -> (name, arguments, ctx) | list
Returning a list short-circuits the pipeline with an error response.
"""

import json
import sys
import time
from pathlib import Path
from collections import deque
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

# Ensure project root is on sys.path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.middleware import (
    DispatchContext,
    unwrap_kwargs,
    resolve_alias,
    validate_params,
    check_rate_limit,
    _tool_call_history,
)
from src.mcp_handlers.tool_stability import resolve_tool_alias, _TOOL_ALIASES



# ============================================================================
# Helpers
# ============================================================================

def _make_ctx(**kwargs) -> DispatchContext:
    """Create a DispatchContext with optional overrides."""
    return DispatchContext(**kwargs)


def _is_short_circuit(result) -> bool:
    """Check if a middleware result is a short-circuit (list of TextContent)."""
    return isinstance(result, list)


def _extract_text(result) -> str:
    """Extract text from a short-circuit result (list of TextContent)."""
    assert isinstance(result, list) and len(result) > 0
    return result[0].text


# ============================================================================
# 1. DispatchContext dataclass
# ============================================================================

class TestDispatchContext:
    """Tests for DispatchContext dataclass."""

    def test_default_values(self):
        ctx = DispatchContext()
        assert ctx.session_key is None
        assert ctx.client_session_id is None
        assert ctx.bound_agent_id is None
        assert ctx.context_token is None
        assert ctx.trajectory_confidence_token is None
        assert ctx.migration_note is None
        assert ctx.original_name is None
        assert ctx.client_hint is None
        assert ctx.identity_result is None

    def test_all_fields_settable(self):
        ctx = DispatchContext(
            session_key="sk-123",
            client_session_id="cs-456",
            bound_agent_id="agent-789",
            context_token="tok",
            trajectory_confidence_token="traj-tok",
            migration_note="Use new_tool instead",
            original_name="old_tool",
            client_hint="cursor",
            identity_result={"agent_uuid": "abc"},
        )
        assert ctx.session_key == "sk-123"
        assert ctx.client_session_id == "cs-456"
        assert ctx.bound_agent_id == "agent-789"
        assert ctx.context_token == "tok"
        assert ctx.trajectory_confidence_token == "traj-tok"
        assert ctx.migration_note == "Use new_tool instead"
        assert ctx.original_name == "old_tool"
        assert ctx.client_hint == "cursor"
        assert ctx.identity_result == {"agent_uuid": "abc"}

    def test_partial_fields(self):
        ctx = DispatchContext(session_key="key1", bound_agent_id="agent-1")
        assert ctx.session_key == "key1"
        assert ctx.bound_agent_id == "agent-1"
        assert ctx.client_session_id is None


# ============================================================================
# 2. unwrap_kwargs middleware
# ============================================================================

class TestUnwrapKwargs:
    """Tests for the unwrap_kwargs middleware step."""

    @pytest.mark.asyncio
    async def test_dict_kwargs_unwrapped(self):
        """Dict kwargs: {"kwargs": {"foo": "bar"}} -> {"foo": "bar"}"""
        ctx = _make_ctx()
        name, args, ctx_out = await unwrap_kwargs(
            "some_tool", {"kwargs": {"foo": "bar"}}, ctx
        )
        assert name == "some_tool"
        assert args == {"foo": "bar"}
        assert "kwargs" not in args

    @pytest.mark.asyncio
    async def test_string_kwargs_unwrapped(self):
        """String kwargs: {"kwargs": '{"foo": "bar"}'} -> {"foo": "bar"}"""
        ctx = _make_ctx()
        name, args, ctx_out = await unwrap_kwargs(
            "some_tool", {"kwargs": '{"foo": "bar"}'}, ctx
        )
        assert args == {"foo": "bar"}
        assert "kwargs" not in args

    @pytest.mark.asyncio
    async def test_invalid_string_kwargs_stays(self):
        """Invalid JSON string kwargs stays as-is."""
        ctx = _make_ctx()
        name, args, ctx_out = await unwrap_kwargs(
            "some_tool", {"kwargs": "not valid json"}, ctx
        )
        # The invalid string stays in kwargs since parsing failed
        assert args == {"kwargs": "not valid json"}

    @pytest.mark.asyncio
    async def test_no_kwargs_key_passthrough(self):
        """No kwargs key: pass-through unchanged."""
        ctx = _make_ctx()
        original = {"agent_id": "abc", "complexity": 0.5}
        name, args, ctx_out = await unwrap_kwargs("tool", dict(original), ctx)
        assert args == original

    @pytest.mark.asyncio
    async def test_kwargs_merged_with_existing_args(self):
        """Kwargs merged with existing arguments."""
        ctx = _make_ctx()
        name, args, ctx_out = await unwrap_kwargs(
            "tool", {"existing_key": "keep_me", "kwargs": {"new_key": "added"}}, ctx
        )
        assert args["existing_key"] == "keep_me"
        assert args["new_key"] == "added"
        assert "kwargs" not in args

    @pytest.mark.asyncio
    async def test_kwargs_override_existing_args(self):
        """If kwargs contain a key that already exists, kwargs value wins (update semantics)."""
        ctx = _make_ctx()
        name, args, ctx_out = await unwrap_kwargs(
            "tool", {"key": "original", "kwargs": {"key": "from_kwargs"}}, ctx
        )
        assert args["key"] == "from_kwargs"

    @pytest.mark.asyncio
    async def test_empty_dict_kwargs(self):
        """Empty dict kwargs."""
        ctx = _make_ctx()
        name, args, ctx_out = await unwrap_kwargs(
            "tool", {"kwargs": {}}, ctx
        )
        assert args == {}

    @pytest.mark.asyncio
    async def test_string_kwargs_non_dict_json(self):
        """String kwargs that parse to a non-dict (e.g. a list) stay as-is."""
        ctx = _make_ctx()
        name, args, ctx_out = await unwrap_kwargs(
            "tool", {"kwargs": '[1, 2, 3]'}, ctx
        )
        # json.loads('[1,2,3]') returns a list, not dict, so it stays
        assert args == {"kwargs": '[1, 2, 3]'}


# ============================================================================
# 3. resolve_alias middleware
# ============================================================================

class TestResolveAlias:
    """Tests for the resolve_alias middleware step."""

    @pytest.mark.asyncio
    async def test_known_alias_resolves(self):
        """Known alias maps to correct tool name."""
        ctx = _make_ctx()
        name, args, ctx_out = await resolve_alias("status", {}, ctx)
        assert name == "get_governance_metrics"
        assert ctx_out.migration_note is not None
        assert ctx_out.original_name == "status"

    @pytest.mark.asyncio
    async def test_unknown_tool_passthrough(self):
        """Unknown tool passes through unchanged."""
        ctx = _make_ctx()
        name, args, ctx_out = await resolve_alias("nonexistent_tool_xyz", {"foo": 1}, ctx)
        assert name == "nonexistent_tool_xyz"
        assert args == {"foo": 1}
        assert ctx_out.migration_note is None
        assert ctx_out.original_name == "nonexistent_tool_xyz"

    @pytest.mark.asyncio
    async def test_inject_action_adds_action(self):
        """inject_action adds action parameter when not present."""
        pytest.importorskip("unitares_pi_plugin")
        import unitares_pi_plugin as _plugin
        _plugin.register()  # ensures pi_health alias is present
        ctx = _make_ctx()
        # pi_health has inject_action="health"
        name, args, ctx_out = await resolve_alias("pi_health", {}, ctx)
        assert name == "pi"
        assert args.get("action") == "health"

    @pytest.mark.asyncio
    async def test_inject_action_does_not_override(self):
        """inject_action does not override existing action parameter."""
        pytest.importorskip("unitares_pi_plugin")
        import unitares_pi_plugin as _plugin
        _plugin.register()
        ctx = _make_ctx()
        name, args, ctx_out = await resolve_alias(
            "pi_health", {"action": "custom_action"}, ctx
        )
        assert name == "pi"
        assert args["action"] == "custom_action"

    @pytest.mark.asyncio
    async def test_multiple_aliases_for_same_target(self):
        """Multiple aliases can map to the same target tool."""
        ctx1 = _make_ctx()
        name1, _, _ = await resolve_alias("start", {}, ctx1)

        ctx2 = _make_ctx()
        name2, _, _ = await resolve_alias("init", {}, ctx2)

        ctx3 = _make_ctx()
        name3, _, _ = await resolve_alias("register", {}, ctx3)

        assert name1 == "onboard"
        assert name2 == "onboard"
        assert name3 == "onboard"

    @pytest.mark.asyncio
    async def test_consolidated_alias_with_action(self):
        """Consolidated aliases inject action parameter."""
        ctx = _make_ctx()
        name, args, _ = await resolve_alias("list_agents", {}, ctx)
        assert name == "agent"
        assert args["action"] == "list"

    @pytest.mark.asyncio
    async def test_original_name_is_always_set(self):
        """original_name is set regardless of alias match."""
        ctx = _make_ctx()
        _, _, ctx_out = await resolve_alias("health_check", {}, ctx)
        assert ctx_out.original_name == "health_check"




class TestCheckRateLimit:
    """Tests for the check_rate_limit middleware step."""

    @pytest.fixture(autouse=True)
    def clear_history(self):
        """Clear tool call history before each test."""
        _tool_call_history.clear()
        yield
        _tool_call_history.clear()

    @pytest.mark.asyncio
    async def test_rate_limiter_allows_passthrough(self):
        """Rate limiter allows normal requests."""
        ctx = _make_ctx()
        result = await check_rate_limit(
            "process_agent_update", {"agent_id": "test-agent"}, ctx
        )
        assert not _is_short_circuit(result)
        name, args, ctx_out = result
        assert name == "process_agent_update"

    @pytest.mark.asyncio
    async def test_read_only_tools_skip_rate_limiting(self):
        """Read-only tools skip general rate limiting."""
        ctx = _make_ctx()
        result = await check_rate_limit("health_check", {}, ctx)
        assert not _is_short_circuit(result)

        result = await check_rate_limit("get_server_info", {}, ctx)
        assert not _is_short_circuit(result)

        result = await check_rate_limit("list_tools", {}, ctx)
        assert not _is_short_circuit(result)

        result = await check_rate_limit("get_thresholds", {}, ctx)
        assert not _is_short_circuit(result)

    @pytest.mark.asyncio
    async def test_loop_detection_for_expensive_reads(self):
        """Loop detection triggers for list_agents after 20+ calls in 60 seconds."""
        ctx = _make_ctx()

        # Fill the history with 20 recent timestamps
        now = time.time()
        history = _tool_call_history["list_agents"]
        for i in range(20):
            history.append(now - 1)  # All within last 60 seconds

        # Next call should trigger loop detection
        result = await check_rate_limit("list_agents", {}, ctx)
        assert _is_short_circuit(result)
        text = _extract_text(result)
        assert "loop detected" in text.lower() or "rate limit" in text.lower()

    @pytest.mark.asyncio
    async def test_loop_detection_old_calls_expire(self):
        """Old calls outside the 60-second window are cleaned up."""
        ctx = _make_ctx()
        history = _tool_call_history["list_agents"]

        # Add 25 calls that are all older than 60 seconds
        old_time = time.time() - 120
        for i in range(25):
            history.append(old_time)

        # Should pass since old calls are cleaned up
        result = await check_rate_limit("list_agents", {}, ctx)
        assert not _is_short_circuit(result)

    @pytest.mark.asyncio
    async def test_non_expensive_tool_no_loop_detection(self):
        """Non-expensive tools do not trigger loop detection."""
        ctx = _make_ctx()
        # Fill history for a non-expensive tool
        now = time.time()
        history = _tool_call_history["health_check"]
        for i in range(30):
            history.append(now - 1)

        # health_check is read-only and not in expensive_read_only_tools
        result = await check_rate_limit("health_check", {}, ctx)
        assert not _is_short_circuit(result)


class TestPerCallerRateBuckets:
    """Per-caller bucketing: one anonymous flood must not lock out other
    callers' onboard() (the 2026-06-12 bootstrap-lockout incident)."""

    @pytest.fixture(autouse=True)
    def fresh_limiter_and_history(self):
        from src.rate_limiter import get_rate_limiter
        get_rate_limiter().reset()
        _tool_call_history.clear()
        yield
        get_rate_limiter().reset()
        _tool_call_history.clear()

    def _set_signals(self, fp):
        from src.mcp_handlers.context import set_session_signals, SessionSignals
        return set_session_signals(SessionSignals(ip_ua_fingerprint=fp))

    @pytest.mark.asyncio
    async def test_flooded_fingerprint_does_not_block_other_callers(self):
        """Exhausting one unbound caller's bucket leaves other unbound
        callers — including a fresh session calling onboard — unaffected."""
        from src.mcp_handlers.context import reset_session_signals
        from src.rate_limiter import get_rate_limiter
        ctx = _make_ctx()

        tok = self._set_signals("127.0.0.1:f100dd")
        try:
            limiter = get_rate_limiter()
            # Saturate the flooding caller's per-minute budget directly.
            for _ in range(limiter.max_per_minute):
                limiter.check_rate_limit("anon:127.0.0.1:f100dd")
            result = await check_rate_limit("onboard", {}, ctx)
            assert _is_short_circuit(result), "flooding caller should be limited"
        finally:
            reset_session_signals(tok)

        tok = self._set_signals("127.0.0.1:a200bb")
        try:
            result = await check_rate_limit("onboard", {}, ctx)
            assert not _is_short_circuit(result), (
                "a different caller must not inherit the flooder's bucket"
            )
        finally:
            reset_session_signals(tok)

    @pytest.mark.asyncio
    async def test_unbound_call_buckets_on_fingerprint(self):
        """Unbound calls are keyed anon:<ip_ua_fingerprint>, not 'anonymous'."""
        from src.mcp_handlers.context import reset_session_signals
        from src.rate_limiter import get_rate_limiter
        ctx = _make_ctx()
        tok = self._set_signals("127.0.0.1:b300cc")
        try:
            result = await check_rate_limit("process_agent_update", {}, ctx)
            assert not _is_short_circuit(result)
            stats = get_rate_limiter().get_stats("anon:127.0.0.1:b300cc")
            assert stats["requests_last_minute"] == 1
        finally:
            reset_session_signals(tok)

    @pytest.mark.asyncio
    async def test_bound_agent_without_injected_arg_uses_bound_id(self):
        """When inject_identity skipped injection, ctx.bound_agent_id keys
        the bucket (attribution to the bound agent, not 'anonymous')."""
        from src.rate_limiter import get_rate_limiter
        ctx = _make_ctx(bound_agent_id="agent-bound-1")
        result = await check_rate_limit("process_agent_update", {}, ctx)
        assert not _is_short_circuit(result)
        stats = get_rate_limiter().get_stats("agent-bound-1")
        assert stats["requests_last_minute"] == 1

    @pytest.mark.asyncio
    async def test_pre_onboard_reads_skip_general_limit(self):
        """Declared pre_onboard_actions reads (knowledge search, agent list,
        dialectic get, ...) skip the general limiter even for a saturated
        caller; writes on the same tools do not."""
        from src.mcp_handlers.context import reset_session_signals
        from src.rate_limiter import get_rate_limiter
        import src.mcp_handlers  # noqa: F401 — ensure tool registration
        ctx = _make_ctx()
        tok = self._set_signals("127.0.0.1:c400dd")
        try:
            limiter = get_rate_limiter()
            for _ in range(limiter.max_per_minute):
                limiter.check_rate_limit("anon:127.0.0.1:c400dd")
            result = await check_rate_limit("knowledge", {"action": "search"}, ctx)
            assert not _is_short_circuit(result), "knowledge search is a pre-onboard read"
            result = await check_rate_limit("dialectic", {"action": "get"}, ctx)
            assert not _is_short_circuit(result), "dialectic get is a pre-onboard read"
            result = await check_rate_limit("knowledge", {"action": "store"}, ctx)
            assert _is_short_circuit(result), "knowledge store must stay limited"
        finally:
            reset_session_signals(tok)

    @pytest.mark.asyncio
    async def test_loop_detection_matches_canonical_agent_list(self):
        """list_agents arrives at this step as agent(action=list) after
        resolve_alias — loop detection must key on the canonical call."""
        ctx = _make_ctx()
        now = time.time()
        history = _tool_call_history["agent:list"]
        for _ in range(20):
            history.append(now - 1)
        result = await check_rate_limit("agent", {"action": "list"}, ctx)
        assert _is_short_circuit(result)
        text = _extract_text(result)
        assert "loop detected" in text.lower()


class TestResolveToolAlias:
    """Direct unit tests for resolve_tool_alias function."""

    def test_known_alias(self):
        actual, alias_info = resolve_tool_alias("status")
        assert actual == "get_governance_metrics"
        assert alias_info is not None
        assert alias_info.old_name == "status"

    def test_unknown_tool(self):
        actual, alias_info = resolve_tool_alias("completely_unknown")
        assert actual == "completely_unknown"
        assert alias_info is None

    def test_start_maps_to_onboard(self):
        actual, alias_info = resolve_tool_alias("start")
        assert actual == "onboard"

    def test_pi_health_inject_action(self):
        pytest.importorskip("unitares_pi_plugin")
        import unitares_pi_plugin as _plugin
        _plugin.register()
        actual, alias_info = resolve_tool_alias("pi_health")
        assert actual == "pi"
        assert alias_info.inject_action == "health"

    def test_list_agents_maps_to_agent(self):
        actual, alias_info = resolve_tool_alias("list_agents")
        assert actual == "agent"
        assert alias_info.inject_action == "list"


# ============================================================================
# 8. dispatch_tool integration tests
# ============================================================================

class TestDispatchToolIntegration:
    """Integration tests for dispatch_tool with mocked identity and handlers."""

    @pytest.fixture
    def mock_identity_pipeline(self):
        """Mock the identity-related middleware steps to avoid DB/Redis deps."""
        with patch(
            "src.mcp_handlers.middleware.resolve_identity",
            new_callable=AsyncMock,
        ) as mock_resolve, patch(
            "src.mcp_handlers.middleware.verify_trajectory",
            new_callable=AsyncMock,
        ) as mock_verify:
            async def fake_resolve_identity(name, arguments, ctx):
                ctx.session_key = "test-session"
                ctx.bound_agent_id = "test-agent-uuid"
                ctx.identity_result = {
                    "agent_uuid": "test-agent-uuid",
                    "created": False,
                    "persisted": True,
                }
                # Set the context so inject_identity can find it
                from src.mcp_handlers.context import set_session_context
                ctx.context_token = set_session_context(
                    session_key="test-session",
                    agent_id="test-agent-uuid",
                )
                return name, arguments, ctx

            async def fake_verify_trajectory(name, arguments, ctx):
                return name, arguments, ctx

            mock_resolve.side_effect = fake_resolve_identity
            mock_verify.side_effect = fake_verify_trajectory

            yield mock_resolve, mock_verify

    @pytest.fixture
    def mock_track_patterns(self):
        """Mock track_patterns to avoid pattern_tracker dependency."""
        with patch(
            "src.mcp_handlers.middleware.track_patterns",
            new_callable=AsyncMock,
        ) as mock_patterns:
            async def fake_track(name, arguments, ctx):
                return name, arguments, ctx
            mock_patterns.side_effect = fake_track
            yield mock_patterns

    @pytest.fixture
    def clean_rate_limit(self):
        """Clear rate limit history and reset rate limiter."""
        from src.rate_limiter import get_rate_limiter
        get_rate_limiter().reset()
        _tool_call_history.clear()
        yield
        get_rate_limiter().reset()
        _tool_call_history.clear()

    @pytest.mark.asyncio
    async def test_known_tool_dispatches(self, mock_identity_pipeline, mock_track_patterns, clean_rate_limit):
        """Known tool dispatches correctly and returns result."""
        from src.mcp_handlers import dispatch_tool, TOOL_HANDLERS

        # Pick a tool we know exists in the registry
        if "health_check" not in TOOL_HANDLERS:
            pytest.skip("health_check not in TOOL_HANDLERS")

        # Mock the handler to return a predictable result
        from mcp.types import TextContent
        expected = [TextContent(type="text", text='{"status": "ok"}')]
        original_handler = TOOL_HANDLERS["health_check"]
        TOOL_HANDLERS["health_check"] = AsyncMock(return_value=expected)
        try:
            result = await dispatch_tool("health_check", {})
            assert result == expected
        finally:
            TOOL_HANDLERS["health_check"] = original_handler

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, mock_identity_pipeline, mock_track_patterns, clean_rate_limit):
        """Unknown tool returns tool_not_found error."""
        from src.mcp_handlers import dispatch_tool

        result = await dispatch_tool("absolutely_nonexistent_tool_xyz", {})
        assert result is not None
        assert len(result) > 0
        text = result[0].text
        assert "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_handler_exception_caught(self, mock_identity_pipeline, mock_track_patterns, clean_rate_limit):
        """Handler exception caught by @mcp_tool decorator returns error response."""
        from src.mcp_handlers import dispatch_tool, TOOL_HANDLERS
        from src.mcp_handlers.decorators import mcp_tool

        # Create a properly decorated handler that raises
        @mcp_tool("_test_failing_tool", timeout=5.0, register=False)
        async def handle_test_failing(arguments):
            raise RuntimeError("test boom")

        original = TOOL_HANDLERS.get("health_check")
        TOOL_HANDLERS["_test_failing_tool"] = handle_test_failing
        try:
            result = await dispatch_tool("_test_failing_tool", {})
            # The @mcp_tool decorator wraps handlers with try/except,
            # so the exception is caught and returned as an error response
            assert result is not None
            assert len(result) > 0
            text = result[0].text
            assert "error" in text.lower() or "boom" in text.lower()
        finally:
            TOOL_HANDLERS.pop("_test_failing_tool", None)

    @pytest.mark.asyncio
    async def test_alias_resolution_end_to_end(self, mock_identity_pipeline, mock_track_patterns, clean_rate_limit):
        """Alias resolution works end-to-end through dispatch_tool."""
        from src.mcp_handlers import dispatch_tool, TOOL_HANDLERS

        # "status" is an alias for "get_governance_metrics"
        if "get_governance_metrics" not in TOOL_HANDLERS:
            pytest.skip("get_governance_metrics not in TOOL_HANDLERS")

        from mcp.types import TextContent
        expected = [TextContent(type="text", text='{"resolved": "via_alias"}')]
        original = TOOL_HANDLERS["get_governance_metrics"]
        TOOL_HANDLERS["get_governance_metrics"] = AsyncMock(return_value=expected)
        try:
            result = await dispatch_tool("status", {})
            assert result == expected
        finally:
            TOOL_HANDLERS["get_governance_metrics"] = original

    @pytest.mark.asyncio
    async def test_kwargs_unwrapping_end_to_end(self, mock_identity_pipeline, mock_track_patterns, clean_rate_limit):
        """kwargs unwrapping works through the full pipeline."""
        from src.mcp_handlers import dispatch_tool, TOOL_HANDLERS

        if "health_check" not in TOOL_HANDLERS:
            pytest.skip("health_check not in TOOL_HANDLERS")

        from mcp.types import TextContent
        expected = [TextContent(type="text", text='{"unwrapped": true}')]
        original = TOOL_HANDLERS["health_check"]

        captured_args = {}

        async def capture_handler(arguments):
            captured_args.update(arguments)
            return expected

        TOOL_HANDLERS["health_check"] = capture_handler
        try:
            result = await dispatch_tool(
                "health_check",
                {"kwargs": {"custom_param": "value"}}
            )
            assert result == expected
            assert captured_args.get("custom_param") == "value"
            assert "kwargs" not in captured_args
        finally:
            TOOL_HANDLERS["health_check"] = original

    @pytest.mark.asyncio
    async def test_none_arguments_defaults_to_empty_dict(self, mock_identity_pipeline, mock_track_patterns, clean_rate_limit):
        """None arguments are converted to empty dict."""
        from src.mcp_handlers import dispatch_tool, TOOL_HANDLERS

        if "health_check" not in TOOL_HANDLERS:
            pytest.skip("health_check not in TOOL_HANDLERS")

        from mcp.types import TextContent
        expected = [TextContent(type="text", text='{}')]

        captured_args = {}

        async def capture_handler(arguments):
            captured_args.update(arguments)
            return expected

        original = TOOL_HANDLERS["health_check"]
        TOOL_HANDLERS["health_check"] = capture_handler
        try:
            result = await dispatch_tool("health_check", None)
            assert result == expected
        finally:
            TOOL_HANDLERS["health_check"] = original

    @pytest.mark.asyncio
    async def test_consolidated_alias_injects_action(self, mock_identity_pipeline, mock_track_patterns, clean_rate_limit):
        """Consolidated alias (e.g., list_agents -> agent(action='list')) injects action param."""
        from src.mcp_handlers import dispatch_tool, TOOL_HANDLERS

        if "agent" not in TOOL_HANDLERS:
            pytest.skip("agent not in TOOL_HANDLERS")

        from mcp.types import TextContent
        expected = [TextContent(type="text", text='{"action": "list"}')]

        captured_args = {}

        async def capture_handler(arguments):
            captured_args.update(arguments)
            return expected

        original = TOOL_HANDLERS["agent"]
        TOOL_HANDLERS["agent"] = capture_handler
        try:
            result = await dispatch_tool("list_agents", {})
            assert captured_args.get("action") == "list"
        finally:
            TOOL_HANDLERS["agent"] = original


# ============================================================================
# 9. inject_identity middleware
# ============================================================================

class TestInjectIdentity:
    """Tests for the inject_identity middleware step."""

    @pytest.mark.asyncio
    async def test_bound_id_injected_for_regular_tool(self):
        """When bound_id exists and no agent_id provided, injects it."""
        from src.mcp_handlers.middleware import inject_identity
        ctx = _make_ctx(bound_agent_id="bound-uuid-1234")
        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="bound-uuid-1234"):
            result = await inject_identity("process_agent_update", {}, ctx)
        assert not _is_short_circuit(result)
        name, args, ctx_out = result
        assert args["agent_id"] == "bound-uuid-1234"

    @pytest.mark.asyncio
    async def test_browsable_tools_skip_injection(self):
        """Browsable data tools do NOT auto-filter by agent_id."""
        from src.mcp_handlers.middleware import inject_identity
        browsable = ["search_knowledge_graph", "list_knowledge_graph",
                      "list_dialectic_sessions", "get_dialectic_session", "dialectic"]
        for tool_name in browsable:
            ctx = _make_ctx(bound_agent_id="bound-uuid-1234")
            with patch("src.mcp_handlers.context.get_context_agent_id", return_value="bound-uuid-1234"):
                result = await inject_identity(tool_name, {}, ctx)
            assert not _is_short_circuit(result)
            _, args, _ = result
            assert "agent_id" not in args, f"{tool_name} should not inject agent_id"

    @pytest.mark.asyncio
    async def test_impersonation_blocked(self):
        """Different agent_id than bound → error (session mismatch)."""
        from src.mcp_handlers.middleware import inject_identity
        ctx = _make_ctx(bound_agent_id="bound-uuid-1234")
        # Mock get_mcp_server to return empty metadata (no label match)
        mock_server = MagicMock()
        mock_server.agent_metadata = {}
        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="bound-uuid-1234"):
            with patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
                result = await inject_identity(
                    "process_agent_update",
                    {"agent_id": "different-uuid"},
                    ctx
                )
        assert _is_short_circuit(result)
        text = _extract_text(result)
        assert "mismatch" in text.lower()

    @pytest.mark.asyncio
    async def test_dialectic_tools_allow_different_id(self):
        """Dialectic tools allow different agent_id (for cross-agent review)."""
        from src.mcp_handlers.middleware import inject_identity
        ctx = _make_ctx(bound_agent_id="bound-uuid-1234")
        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="bound-uuid-1234"):
            result = await inject_identity(
                "submit_thesis",
                {"agent_id": "other-uuid"},
                ctx
            )
        assert not _is_short_circuit(result)

    @pytest.mark.asyncio
    async def test_no_binding_provided_id_passthrough(self):
        """No session binding but agent_id provided → passes through."""
        from src.mcp_handlers.middleware import inject_identity
        ctx = _make_ctx()
        with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
            result = await inject_identity(
                "process_agent_update",
                {"agent_id": "direct-uuid"},
                ctx
            )
        assert not _is_short_circuit(result)
        _, args, _ = result
        assert args["agent_id"] == "direct-uuid"

    @pytest.mark.asyncio
    async def test_no_binding_no_id_identity_tools_ok(self):
        """Identity tools work without any binding or agent_id."""
        from src.mcp_handlers.middleware import inject_identity
        identity_tools = ["status", "list_tools", "health_check", "onboard", "identity"]
        for tool_name in identity_tools:
            ctx = _make_ctx()
            with patch("src.mcp_handlers.context.get_context_agent_id", return_value=None):
                result = await inject_identity(tool_name, {}, ctx)
            assert not _is_short_circuit(result), f"{tool_name} should not short-circuit"

    @pytest.mark.asyncio
    async def test_exception_skips_gracefully(self):
        """If context lookup throws, middleware continues gracefully."""
        from src.mcp_handlers.middleware import inject_identity
        ctx = _make_ctx()
        with patch("src.mcp_handlers.context.get_context_agent_id", side_effect=RuntimeError("test error")):
            result = await inject_identity("process_agent_update", {}, ctx)
        assert not _is_short_circuit(result)

    @pytest.mark.asyncio
    async def test_label_match_allows_different_id(self):
        """Label match allows using a different agent_id."""
        from src.mcp_handlers.middleware import inject_identity
        ctx = _make_ctx(bound_agent_id="bound-uuid-1234")
        mock_server = MagicMock()
        mock_meta = MagicMock()
        mock_meta.label = "my-friendly-name"
        mock_server.agent_metadata = {"bound-uuid-1234": mock_meta}
        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="bound-uuid-1234"):
            with patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
                result = await inject_identity(
                    "process_agent_update",
                    {"agent_id": "my-friendly-name"},
                    ctx
                )
        assert not _is_short_circuit(result)

    @pytest.mark.asyncio
    async def test_public_or_structured_alias_match_allows_different_id(self):
        """Public/structured aliases should be accepted for the bound UUID."""
        from src.mcp_handlers.middleware import inject_identity

        ctx = _make_ctx(bound_agent_id="bound-uuid-1234")
        mock_server = MagicMock()
        mock_meta = MagicMock()
        mock_meta.label = "my-friendly-name"
        mock_meta.public_agent_id = "Gpt_5_Codex_20260404"
        mock_meta.structured_id = "mcp_20260404_5"
        mock_server.agent_metadata = {"bound-uuid-1234": mock_meta}

        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="bound-uuid-1234"), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_server):
            for alias in ("Gpt_5_Codex_20260404", "mcp_20260404_5"):
                result = await inject_identity(
                    "process_agent_update",
                    {"agent_id": alias},
                    ctx,
                )
                assert not _is_short_circuit(result)

    @pytest.mark.asyncio
    async def test_bind_session_skips_injection(self):
        """bind_session handles its own identity — no auto-injection."""
        from src.mcp_handlers.middleware import inject_identity
        ctx = _make_ctx(bound_agent_id="auto-created-uuid")
        with patch("src.mcp_handlers.context.get_context_agent_id", return_value="auto-created-uuid"):
            result = await inject_identity("bind_session", {}, ctx)
        assert not _is_short_circuit(result)
        _, args, _ = result
        assert "agent_id" not in args, "bind_session should not get auto-injected agent_id"


# ============================================================================
# 10. track_patterns middleware
# ============================================================================

class TestTrackPatterns:
    """Tests for the track_patterns middleware step."""

    @pytest.mark.asyncio
    async def test_passes_through_normally(self):
        """Pattern tracking passes through without blocking."""
        from src.mcp_handlers.middleware import track_patterns
        from src.mcp_handlers import utils as mcp_utils
        ctx = _make_ctx()
        mock_tracker = MagicMock()
        mock_tracker.record_tool_call.return_value = None
        mock_tracker.record_progress.return_value = None
        # Inject get_bound_agent_id into utils module (it's missing there, import is broken in prod)
        mock_get_bound = MagicMock(return_value="test-agent")
        mcp_utils.get_bound_agent_id = mock_get_bound
        try:
            with patch("src.pattern_tracker.get_pattern_tracker", return_value=mock_tracker):
                with patch("src.mcp_handlers.support.pattern_helpers.record_hypothesis_if_needed"):
                    with patch("src.mcp_handlers.support.pattern_helpers.check_untested_hypotheses", return_value=None):
                        with patch("src.mcp_handlers.support.pattern_helpers.mark_hypothesis_tested"):
                            result = await track_patterns("process_agent_update", {"agent_id": "test-agent"}, ctx)
        finally:
            if hasattr(mcp_utils, 'get_bound_agent_id'):
                delattr(mcp_utils, 'get_bound_agent_id')
        assert not _is_short_circuit(result)
        mock_tracker.record_tool_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_in_tracking_does_not_block(self):
        """Exception in pattern tracking does not block the pipeline."""
        from src.mcp_handlers.middleware import track_patterns
        ctx = _make_ctx()
        with patch("src.pattern_tracker.get_pattern_tracker", side_effect=ImportError("not available")):
            result = await track_patterns("process_agent_update", {}, ctx)
        assert not _is_short_circuit(result)

    @pytest.mark.asyncio
    async def test_no_agent_id_skips_tracking(self):
        """When no agent_id can be resolved, tracking is skipped."""
        from src.mcp_handlers.middleware import track_patterns
        from src.mcp_handlers import utils as mcp_utils
        ctx = _make_ctx()
        mock_tracker = MagicMock()
        # Inject get_bound_agent_id returning None
        mcp_utils.get_bound_agent_id = MagicMock(return_value=None)
        try:
            with patch("src.pattern_tracker.get_pattern_tracker", return_value=mock_tracker):
                result = await track_patterns("health_check", {}, ctx)
        finally:
            if hasattr(mcp_utils, 'get_bound_agent_id'):
                delattr(mcp_utils, 'get_bound_agent_id')
        assert not _is_short_circuit(result)
        mock_tracker.record_tool_call.assert_not_called()
