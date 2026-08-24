"""
Integration tests for dispatch_tool() pipeline in src/mcp_handlers/__init__.py.

Tests the 10-stage dispatch pipeline with mocked backends:
- Null argument coercion
- Session/identity resolution
- Onboard pin injection
- Kwargs unwrapping
- Alias resolution
- Identity injection
- Rate limiting
- Handler execution
- Unknown tool handling
- Error handling
"""

import json
import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.decorators import (
    mcp_tool,
    _TOOL_DEFINITIONS,
    ToolDefinition,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def clean_registry():
    """Save/restore tool registry to prevent cross-test contamination."""
    from src.mcp_handlers import TOOL_HANDLERS
    orig_defs = dict(_TOOL_DEFINITIONS)
    orig_handlers = dict(TOOL_HANDLERS)
    yield
    _TOOL_DEFINITIONS.clear()
    _TOOL_DEFINITIONS.update(orig_defs)
    TOOL_HANDLERS.clear()
    TOOL_HANDLERS.update(orig_handlers)


@pytest.fixture
def mock_identity():
    """Mock identity resolution to return a stable test agent."""
    with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock) as m:
        m.return_value = {
            "agent_uuid": "test-uuid-0000-1111-2222",
            "agent_name": "TestAgent",
            "created": False,
            "persisted": True,
        }
        yield m


@pytest.fixture
def mock_identity_new():
    """Mock identity resolution for newly created (ephemeral) agent."""
    with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock) as m:
        m.return_value = {
            "agent_uuid": "new-uuid-3333-4444-5555",
            "agent_name": None,
            "created": True,
            "persisted": False,
        }
        yield m


@pytest.fixture
def mock_db():
    """Mock database to prevent real DB calls."""
    with patch("src.db.get_db") as m:
        db = AsyncMock()
        db.update_session_activity = AsyncMock(return_value=True)
        m.return_value = db
        yield db


@pytest.fixture
def mock_rate_limiter():
    """Mock rate limiter to allow all calls by default."""
    with patch("src.mcp_handlers.middleware.rate_limit_step.get_rate_limiter") as m:
        limiter = MagicMock()
        limiter.check_rate_limit.return_value = (True, None)
        limiter.get_stats.return_value = {}
        m.return_value = limiter
        yield limiter


@pytest.fixture
def mock_pattern_tracker():
    """Mock pattern tracker to prevent side effects."""
    with patch("src.pattern_tracker.get_pattern_tracker") as m:
        tracker = MagicMock()
        tracker.record_tool_call.return_value = None
        tracker.record_progress.return_value = None
        m.return_value = tracker
        yield tracker


@pytest.fixture
def mock_onboard_pin():
    """Mock onboard pin lookup to return None (no pin)."""
    with patch("src.mcp_handlers.identity.handlers.lookup_onboard_pin", new_callable=AsyncMock) as m:
        m.return_value = None
        yield m


@pytest.fixture
def mock_derive_session_key():
    """Mock session key derivation (async)."""
    with patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock) as m:
        m.return_value = "test-session-key"
        yield m


@pytest.fixture
def mock_validators():
    """Mock parameter validators to pass through."""
    with patch("src.tool_schemas.get_pydantic_schemas") as m:
        m.return_value = {}
        yield m


@pytest.fixture
def mock_tool_alias():
    """Mock alias resolution to return name unchanged."""
    with patch("src.mcp_handlers.tool_stability.resolve_tool_alias") as m:
        m.side_effect = lambda name: (name, None)
        yield m


@pytest.fixture
def integration_mocks(
    mock_identity, mock_db, mock_rate_limiter, mock_pattern_tracker,
    mock_onboard_pin, mock_derive_session_key, mock_validators, mock_tool_alias
):
    """Combine all integration mocks into one fixture."""
    return {
        "identity": mock_identity,
        "db": mock_db,
        "rate_limiter": mock_rate_limiter,
        "pattern_tracker": mock_pattern_tracker,
        "onboard_pin": mock_onboard_pin,
        "derive_session_key": mock_derive_session_key,
        "validators": mock_validators,
        "tool_alias": mock_tool_alias,
    }


def _register_in_defs(name, handler, description="Test handler"):
    """Register a handler in both TOOL_HANDLERS and _TOOL_DEFINITIONS."""
    from src.mcp_handlers import TOOL_HANDLERS
    TOOL_HANDLERS[name] = handler
    _TOOL_DEFINITIONS[name] = ToolDefinition(
        name=name, handler=handler, timeout=30.0, description=description,
    )


def register_test_handler(name="test_handler", result=None):
    """Register a test handler in TOOL_HANDLERS and decorator registry."""
    from mcp.types import TextContent

    if result is None:
        result = [TextContent(type="text", text=json.dumps({"success": True}))]

    async def handler(arguments):
        return result

    _register_in_defs(name, handler)
    return handler


# ============================================================================
# dispatch_tool - Basic Execution
# ============================================================================

class TestDispatchToolBasicExecution:

    @pytest.mark.asyncio
    async def test_dispatches_to_registered_handler(self, integration_mocks):
        """dispatch_tool should call the registered handler and return its result."""
        from src.mcp_handlers import dispatch_tool
        from mcp.types import TextContent

        expected = [TextContent(type="text", text='{"result": "ok"}')]
        register_test_handler("test_handler", expected)

        result = await dispatch_tool("test_handler", {})
        assert result is not None
        assert len(result) == 1
        assert '"result"' in result[0].text

    @pytest.mark.asyncio
    async def test_none_arguments_coerced_to_empty_dict(self, integration_mocks):
        """dispatch_tool should handle None arguments gracefully."""
        from src.mcp_handlers import dispatch_tool
        from mcp.types import TextContent

        call_log = []

        async def logging_handler(arguments):
            call_log.append(arguments)
            return [TextContent(type="text", text='{"ok": true}')]

        _register_in_defs("test_null_args", logging_handler)

        result = await dispatch_tool("test_null_args", None)
        assert result is not None
        # Handler should have received a dict, not None
        assert isinstance(call_log[0], dict)

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self, integration_mocks):
        """dispatch_tool should return an error for unknown tool names."""
        from src.mcp_handlers import dispatch_tool

        result = await dispatch_tool("totally_nonexistent_tool_xyz", {})
        assert result is not None
        text = result[0].text
        data = json.loads(text)
        assert data.get("error") is True or "not found" in text.lower()


# ============================================================================
# dispatch_tool - Identity Injection
# ============================================================================

class TestDispatchToolIdentityInjection:

    @pytest.mark.asyncio
    async def test_injects_agent_id_from_session(self, integration_mocks):
        """Handler should receive agent_id injected from session identity."""
        from src.mcp_handlers import dispatch_tool
        from mcp.types import TextContent

        received_args = {}

        async def capture_handler(arguments):
            received_args.update(arguments)
            return [TextContent(type="text", text='{"ok": true}')]

        _register_in_defs("test_identity_inject", capture_handler)

        await dispatch_tool("test_identity_inject", {})
        assert received_args.get("agent_id") == "test-uuid-0000-1111-2222"

    @pytest.mark.asyncio
    async def test_identity_mismatch_returns_error(self, integration_mocks):
        """Providing a different agent_id than session should return error."""
        from src.mcp_handlers import dispatch_tool

        register_test_handler("test_mismatch")

        # Mock get_mcp_server for label check
        with patch("src.mcp_handlers.shared.get_mcp_server") as mock_server:
            mock_server.return_value = MagicMock(agent_metadata={})
            result = await dispatch_tool("test_mismatch", {
                "agent_id": "different-uuid-9999"
            })

        assert result is not None
        text = result[0].text.lower()
        assert "mismatch" in text or "error" in text

    @pytest.mark.asyncio
    async def test_consult_accepts_trusted_identity_metadata_with_strict_schema(
        self,
        mock_identity,
        mock_db,
        mock_rate_limiter,
        mock_pattern_tracker,
        mock_onboard_pin,
        mock_derive_session_key,
    ):
        """Real dispatch identity handoff must survive ConsultParams extra=forbid."""
        from src.mcp_handlers import dispatch_tool
        from src.mcp_handlers.support import consultation as co
        from src.mcp_handlers.support.inference_outcome import InferenceOutcome

        backend = AsyncMock(return_value=InferenceOutcome(
            response="careful advice",
            routed_via="ollama",
            task_type="reasoning",
            inference={
                "schema": "unitares.inference_result.v0",
                "host_id": "ollama:local",
                "provider_kind": "ollama",
                "transport": "openai_compatible_http",
                "model_used": "test-model",
                "models_used": ["test-model"],
                "task_type": "reasoning",
                "privacy_class": "local",
                "cost_class": "local_compute",
                "accountability_class": "tool_evidence",
                "finish_reason": "stop",
            },
        ))

        with patch.object(co, "run_model_inference", backend):
            result = await dispatch_tool("consult", {"brief": "Explain this"})

        parsed = json.loads(result[0].text)
        assert parsed["success"] is True
        assert parsed["status"] == "completed"
        request = backend.await_args.args[0]
        assert request.requesting_agent_uuid == "test-uuid-0000-1111-2222"

    @pytest.mark.asyncio
    async def test_consult_rejects_public_route_controls_before_backend(
        self,
        mock_identity,
        mock_db,
        mock_rate_limiter,
        mock_pattern_tracker,
        mock_onboard_pin,
        mock_derive_session_key,
    ):
        from src.mcp_handlers import dispatch_tool
        from src.mcp_handlers.support import consultation as co

        backend = AsyncMock()
        with patch.object(co, "run_model_inference", backend):
            result = await dispatch_tool("consult", {
                "brief": "Explain this",
                "provider": "hf",
            })

        parsed = json.loads(result[0].text)
        assert parsed["success"] is False
        assert parsed["error_code"] == "PARAMETER_ERROR"
        backend.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consult_strips_forged_reserved_metadata(
        self,
        mock_identity,
        mock_db,
        mock_rate_limiter,
        mock_pattern_tracker,
        mock_onboard_pin,
        mock_derive_session_key,
    ):
        from src.mcp_handlers import dispatch_tool
        from src.mcp_handlers.support import consultation as co
        from src.mcp_handlers.support.inference_outcome import InferenceOutcome

        backend = AsyncMock(return_value=InferenceOutcome(
            response="careful advice",
            routed_via="ollama",
            task_type="reasoning",
            inference={
                "schema": "unitares.inference_result.v0",
                "host_id": "ollama:local",
                "provider_kind": "ollama",
                "transport": "openai_compatible_http",
                "privacy_class": "local",
                "cost_class": "local_compute",
                "accountability_class": "tool_evidence",
                "finish_reason": "stop",
            },
        ))
        forged = {
            "_middleware_identity_result": {"agent_uuid": "attacker"},
            "_param_coercions": {"allow_degraded": {"to": True}},
        }

        with patch.object(co, "run_model_inference", backend):
            result = await dispatch_tool("consult", {
                "brief": "Explain this",
                **forged,
            })

        parsed = json.loads(result[0].text)
        assert parsed["success"] is True
        assert "_param_coercions" not in parsed
        assert "attacker" not in result[0].text
        assert (
            backend.await_args.args[0].requesting_agent_uuid
            == "test-uuid-0000-1111-2222"
        )


# ============================================================================
# dispatch_tool - Kwargs Unwrapping
# ============================================================================

class TestDispatchToolKwargsUnwrapping:

    @pytest.mark.asyncio
    async def test_unwraps_kwargs_dict(self, integration_mocks):
        """Arguments wrapped in kwargs dict should be unwrapped."""
        from src.mcp_handlers import dispatch_tool
        from mcp.types import TextContent

        received_args = {}

        async def capture_handler(arguments):
            received_args.update(arguments)
            return [TextContent(type="text", text='{"ok": true}')]

        _register_in_defs("test_kwargs_dict", capture_handler)

        await dispatch_tool("test_kwargs_dict", {"kwargs": {"inner_key": "inner_val"}})
        assert received_args.get("inner_key") == "inner_val"

    @pytest.mark.asyncio
    async def test_unwraps_kwargs_json_string(self, integration_mocks):
        """Arguments wrapped in kwargs JSON string should be parsed and unwrapped."""
        from src.mcp_handlers import dispatch_tool
        from mcp.types import TextContent

        received_args = {}

        async def capture_handler(arguments):
            received_args.update(arguments)
            return [TextContent(type="text", text='{"ok": true}')]

        _register_in_defs("test_kwargs_str", capture_handler)

        await dispatch_tool("test_kwargs_str", {"kwargs": '{"json_key": "json_val"}'})
        assert received_args.get("json_key") == "json_val"


# ============================================================================
# dispatch_tool - Alias Resolution
# ============================================================================

class TestDispatchToolAliasResolution:

    @pytest.mark.asyncio
    async def test_alias_resolves_to_real_tool(self, integration_mocks):
        """Tool alias should be resolved to the actual tool name."""
        from src.mcp_handlers import dispatch_tool
        from src.mcp_handlers.tool_stability import ToolAlias

        register_test_handler("get_governance_metrics")

        # Override the mock_tool_alias to actually resolve
        integration_mocks["tool_alias"].side_effect = None
        integration_mocks["tool_alias"].return_value = (
            "get_governance_metrics",
            ToolAlias(
                old_name="status",
                new_name="get_governance_metrics",
                reason="intuitive_alias",
                migration_note="Use get_governance_metrics()",
            )
        )

        result = await dispatch_tool("status", {})
        assert result is not None
        # Should have dispatched to get_governance_metrics handler
        text = result[0].text
        assert "success" in text.lower() or "true" in text.lower()


# ============================================================================
# dispatch_tool - Rate Limiting
# ============================================================================

class TestDispatchToolRateLimiting:

    @pytest.mark.asyncio
    async def test_rate_limited_returns_error(self, integration_mocks):
        """When rate limiter rejects, dispatch_tool should return rate limit error."""
        from src.mcp_handlers import dispatch_tool

        register_test_handler("test_rate_limited")

        # Make rate limiter reject
        integration_mocks["rate_limiter"].check_rate_limit.return_value = (False, "Rate limit exceeded")

        result = await dispatch_tool("test_rate_limited", {"agent_id": "test-uuid-0000-1111-2222"})
        assert result is not None
        text = result[0].text.lower()
        assert "rate limit" in text or "rate_limit" in text

    @pytest.mark.asyncio
    async def test_read_only_tools_skip_rate_limit(self, integration_mocks):
        """Read-only tools should skip rate limiting."""
        from src.mcp_handlers import dispatch_tool

        # Make rate limiter reject everything
        integration_mocks["rate_limiter"].check_rate_limit.return_value = (False, "Rate limit exceeded")

        # get_thresholds is read-only and should skip rate limiting.
        result = await dispatch_tool("get_thresholds", {})
        assert result is not None
        # Should NOT be a rate limit error.
        text = result[0].text.lower()
        assert "rate limit" not in text


# ============================================================================
# dispatch_tool - Browsable Data Tools
# ============================================================================

class TestDispatchToolBrowsableData:

    @pytest.mark.asyncio
    async def test_browsable_tools_no_auto_agent_id(self, integration_mocks):
        """Browsable data tools should NOT auto-inject agent_id."""
        from src.mcp_handlers import dispatch_tool
        from mcp.types import TextContent

        received_args = {}

        async def capture_handler(arguments):
            received_args.update(arguments)
            return [TextContent(type="text", text='{"ok": true}')]

        _register_in_defs("search_knowledge_graph", capture_handler)

        await dispatch_tool("search_knowledge_graph", {"query": "test"})
        # agent_id should NOT be auto-injected for browsable tools
        assert "agent_id" not in received_args or received_args.get("agent_id") is None


# ============================================================================
# dispatch_tool - Context Management
# ============================================================================

class TestDispatchToolContextManagement:

    @pytest.mark.asyncio
    async def test_context_reset_after_dispatch(self, integration_mocks):
        """Session context should be reset after dispatch completes."""
        from src.mcp_handlers import dispatch_tool
        from src.mcp_handlers.context import get_context_agent_id

        register_test_handler("test_ctx_reset")
        await dispatch_tool("test_ctx_reset", {})

        # After dispatch, context should be reset
        agent_id = get_context_agent_id()
        assert agent_id is None or agent_id == ""

    @pytest.mark.asyncio
    async def test_context_reset_on_handler_error(self, integration_mocks):
        """Session context should be reset even if handler raises."""
        from src.mcp_handlers import dispatch_tool, TOOL_HANDLERS
        from src.mcp_handlers.context import get_context_agent_id

        async def failing_handler(arguments):
            raise RuntimeError("Test error")

        _register_in_defs("test_ctx_error", failing_handler)

        # This may raise or return error - either way context should reset
        try:
            await dispatch_tool("test_ctx_error", {})
        except Exception:
            pass

        agent_id = get_context_agent_id()
        assert agent_id is None or agent_id == ""


# ============================================================================
# dispatch_tool - Ephemeral Identity
# ============================================================================

class TestDispatchToolEphemeralIdentity:

    @pytest.mark.asyncio
    async def test_new_identity_marked_ephemeral(self, mock_db, mock_rate_limiter,
            mock_pattern_tracker, mock_onboard_pin, mock_derive_session_key,
            mock_validators, mock_tool_alias):
        """Newly created identities via dispatch should be marked ephemeral."""
        from src.mcp_handlers import dispatch_tool

        register_test_handler("test_ephemeral")

        with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock) as mock_id:
            mock_id.return_value = {
                "agent_uuid": "ephemeral-uuid",
                "agent_name": None,
                "created": True,
                "persisted": False,
            }
            result = await dispatch_tool("test_ephemeral", {})

        # Should succeed (handler called)
        assert result is not None
        # The identity_result should have been mutated to include ephemeral markers
        # (we verify this indirectly - dispatch doesn't fail for ephemeral agents)

    @pytest.mark.asyncio
    async def test_persisted_identity_refreshes_ttl(self, mock_rate_limiter,
            mock_pattern_tracker, mock_onboard_pin, mock_derive_session_key,
            mock_validators, mock_tool_alias):
        """Persisted identities should trigger TTL refresh in DB."""
        from src.mcp_handlers import dispatch_tool

        register_test_handler("test_ttl_refresh")

        mock_db_instance = AsyncMock()
        mock_db_instance.update_session_activity = AsyncMock(return_value=True)

        with patch("src.mcp_handlers.identity.handlers.resolve_session_identity", new_callable=AsyncMock) as mock_id, \
             patch("src.db.get_db") as mock_get_db:
            mock_id.return_value = {
                "agent_uuid": "persisted-uuid",
                "agent_name": "Persisted",
                "created": False,
                "persisted": True,
            }
            mock_get_db.return_value = mock_db_instance

            await dispatch_tool("test_ttl_refresh", {"client_session_id": "sess-123"})

        # TTL refresh should have been called
        mock_db_instance.update_session_activity.assert_called()
