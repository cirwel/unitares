"""
Tests for src/mcp_handlers/wrapper_generator.py - Typed wrapper generation.

Tests _json_type_to_python, create_typed_wrapper, _create_simple_wrapper.
"""

import pytest
import asyncio
import inspect
import sys
from pathlib import Path
from typing import Optional, Union

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.support.wrapper_generator import (
    _json_type_to_python,
    create_typed_wrapper,
    _create_simple_wrapper,
    enable_extra_argument_passthrough,
)
from mcp.server.fastmcp.tools.base import Tool


# ============================================================================
# _json_type_to_python
# ============================================================================

class TestJsonTypeToPython:

    def test_string(self):
        assert _json_type_to_python("string") is str

    def test_integer(self):
        assert _json_type_to_python("integer") is int

    def test_number(self):
        assert _json_type_to_python("number") is float

    def test_boolean(self):
        result = _json_type_to_python("boolean")
        assert result == Union[str, bool]

    def test_array(self):
        assert _json_type_to_python("array") is list

    def test_object(self):
        assert _json_type_to_python("object") is dict

    def test_unknown_type_defaults_to_str(self):
        assert _json_type_to_python("foobar") is str

    def test_list_single_type(self):
        result = _json_type_to_python(["string"])
        assert result is str

    def test_list_single_type_with_null(self):
        result = _json_type_to_python(["string", "null"])
        assert result == Optional[str]

    def test_list_two_non_null(self):
        result = _json_type_to_python(["number", "string"])
        assert result == Union[float, str]

    def test_list_two_non_null_with_null(self):
        result = _json_type_to_python(["number", "string", "null"])
        assert result == Optional[Union[float, str]]

    def test_list_three_non_null(self):
        result = _json_type_to_python(["string", "integer", "number"])
        assert result == Union[str, int, float]

    def test_list_only_null(self):
        result = _json_type_to_python(["null"])
        assert result is str

    def test_list_more_than_three_non_null(self):
        # Should fallback to first type
        result = _json_type_to_python(["string", "integer", "number", "boolean"])
        assert result is str


# ============================================================================
# _create_simple_wrapper
# ============================================================================

class TestCreateSimpleWrapper:

    def test_creates_callable(self):
        def get_handler(name):
            async def handler(**kwargs):
                return {"ok": True}
            return handler

        wrapper = _create_simple_wrapper("test_tool", [], get_handler)
        assert callable(wrapper)
        assert wrapper.__name__ == "test_tool"

    def test_has_correct_signature(self):
        def get_handler(name):
            async def handler(**kwargs):
                return {}
            return handler

        param_info = [
            ("name", str, True, None, None),
            ("age", int, False, None, None),
        ]
        wrapper = _create_simple_wrapper("test_tool", param_info, get_handler)
        sig = inspect.signature(wrapper)
        assert "name" in sig.parameters
        assert "age" in sig.parameters
        assert sig.parameters["age"].default is None

    def test_required_param_has_no_default(self):
        def get_handler(name):
            async def handler(**kwargs):
                return {}
            return handler

        param_info = [("required_param", str, True, None, None)]
        wrapper = _create_simple_wrapper("test_tool", param_info, get_handler)
        sig = inspect.signature(wrapper)
        assert sig.parameters["required_param"].default is inspect.Parameter.empty

    def test_wrapper_calls_handler(self):
        call_log = []

        def get_handler(name):
            async def handler(**kwargs):
                call_log.append(kwargs)
                return {"result": "done"}
            return handler

        wrapper = _create_simple_wrapper("test_tool", [], get_handler)
        result = asyncio.run(wrapper(foo="bar"))
        assert result == {"result": "done"}
        assert call_log[0] == {"foo": "bar"}

    def test_wrapper_filters_none_values(self):
        call_log = []

        def get_handler(name):
            async def handler(**kwargs):
                call_log.append(kwargs)
                return {}
            return handler

        wrapper = _create_simple_wrapper("test_tool", [], get_handler)
        asyncio.run(wrapper(a="keep", b=None))
        assert "b" not in call_log[0]
        assert call_log[0] == {"a": "keep"}

    def test_wrapper_unwraps_kwargs_dict(self):
        call_log = []

        def get_handler(name):
            async def handler(**kwargs):
                call_log.append(kwargs)
                return {}
            return handler

        wrapper = _create_simple_wrapper("test_tool", [], get_handler)
        asyncio.run(wrapper(kwargs={"inner_key": "inner_val"}))
        assert call_log[0] == {"inner_key": "inner_val"}

    def test_wrapper_unwraps_kwargs_json_string(self):
        call_log = []

        def get_handler(name):
            async def handler(**kwargs):
                call_log.append(kwargs)
                return {}
            return handler

        wrapper = _create_simple_wrapper("test_tool", [], get_handler)
        asyncio.run(wrapper(kwargs='{"json_key": "json_val"}'))
        assert call_log[0] == {"json_key": "json_val"}

    def test_wrapper_handles_invalid_json_kwargs_string(self):
        call_log = []

        def get_handler(name):
            async def handler(**kwargs):
                call_log.append(kwargs)
                return {}
            return handler

        wrapper = _create_simple_wrapper("test_tool", [], get_handler)
        asyncio.run(wrapper(kwargs="not valid json"))
        # Should pass through as-is since it's a string (not a dict), gets filtered
        assert call_log[0] == {}


# ============================================================================
# create_typed_wrapper
# ============================================================================

class TestCreateTypedWrapper:

    def test_simple_wrapper(self):
        def get_handler(name):
            async def handler(**kwargs):
                return {"tool": name}
            return handler

        schema = {
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"]
        }
        wrapper = create_typed_wrapper("search", schema, get_handler)
        assert wrapper.__name__ == "search"
        assert callable(wrapper)

    def test_empty_schema(self):
        def get_handler(name):
            async def handler(**kwargs):
                return {}
            return handler

        wrapper = create_typed_wrapper("no_params", {}, get_handler)
        assert wrapper.__name__ == "no_params"

    def test_schema_with_optional_params(self):
        def get_handler(name):
            async def handler(**kwargs):
                return kwargs
            return handler

        schema = {
            "properties": {
                "required_field": {"type": "string"},
                "optional_field": {"type": "integer"},
            },
            "required": ["required_field"]
        }
        wrapper = create_typed_wrapper("mixed", schema, get_handler)
        sig = inspect.signature(wrapper)
        assert "required_field" in sig.parameters
        assert "optional_field" in sig.parameters

    def test_wrapper_qualname_set(self):
        def get_handler(name):
            async def handler(**kwargs):
                return {}
            return handler

        wrapper = create_typed_wrapper("my_tool", {}, get_handler)
        assert wrapper.__qualname__ == "my_tool"

    def test_multiple_type_params(self):
        def get_handler(name):
            async def handler(**kwargs):
                return {}
            return handler

        schema = {
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "enabled": {"type": "boolean"},
                "items": {"type": "array"},
                "config": {"type": "object"},
            },
            "required": []
        }
        wrapper = create_typed_wrapper("multi_type", schema, get_handler)
        sig = inspect.signature(wrapper)
        assert len(sig.parameters) == 6

    def test_nullable_ref_param_resolves_to_object(self):
        """Nested Pydantic models arrive as $ref | null, not string.

        Regression guard for onboard.initial_state: if this degrades to str,
        MCP clients cannot send the bootstrap object that the server validator
        expects, so managed sessions mint UUIDs without a synthetic t=0 anchor.
        """
        call_log = []

        def get_handler(name):
            async def handler(**kwargs):
                call_log.append(kwargs)
                return kwargs
            return handler

        schema = {
            "$defs": {
                "BootstrapStateParams": {
                    "type": "object",
                    "properties": {"task_type": {"type": "string"}},
                }
            },
            "properties": {
                "initial_state": {
                    "anyOf": [
                        {"$ref": "#/$defs/BootstrapStateParams"},
                        {"type": "null"},
                    ]
                }
            },
            "required": [],
        }

        wrapper = create_typed_wrapper("onboard", schema, get_handler)
        sig = inspect.signature(wrapper)

        assert sig.parameters["initial_state"].annotation == Optional[dict]

        tool = Tool.from_function(wrapper, structured_output=False)
        asyncio.run(tool.run({"initial_state": {"task_type": "introspection"}}))

        assert call_log == [{"initial_state": {"task_type": "introspection"}}]


# ============================================================================
# FastMCP Context parameter detection
# ============================================================================
# Regression: wrapper_generator previously set only __signature__, leaving
# __annotations__ empty. FastMCP's find_context_parameter reads get_type_hints
# (→ __annotations__) to decide whether to skip `ctx` from the emitted tools/list
# inputSchema, so ctx leaked as a user-facing argument. See KG discovery
# 2026-04-24T00:16:12.229114.

class TestSessionWrapperContextDetection:

    def test_session_wrapper_annotations_match_signature(self):
        """Synthesized signature's annotations must be mirrored in __annotations__."""
        def get_handler(name):
            async def handler(**kwargs):
                return {}
            return handler

        def session_extractor(ctx):
            return "session-id"

        schema = {"properties": {"query": {"type": "string"}}, "required": []}
        wrapper = create_typed_wrapper(
            "session_tool", schema, get_handler,
            inject_session=True, session_extractor=session_extractor,
        )
        sig = inspect.signature(wrapper)
        assert "ctx" in sig.parameters
        # __annotations__ must carry ctx so get_type_hints can find it.
        assert "ctx" in wrapper.__annotations__
        assert wrapper.__annotations__["ctx"] == sig.parameters["ctx"].annotation

    def test_fastmcp_finds_ctx_in_session_wrapper(self):
        """FastMCP's find_context_parameter must locate ctx on a session wrapper."""
        pytest.importorskip("mcp.server.fastmcp")
        from mcp.server.fastmcp.utilities.context_injection import find_context_parameter

        def get_handler(name):
            async def handler(**kwargs):
                return {}
            return handler

        def session_extractor(ctx):
            return "sid"

        schema = {"properties": {"query": {"type": "string"}}, "required": []}
        wrapper = create_typed_wrapper(
            "session_tool", schema, get_handler,
            inject_session=True, session_extractor=session_extractor,
        )
        assert find_context_parameter(wrapper) == "ctx"

    def test_tools_list_schema_omits_ctx(self):
        """With ctx detected, func_metadata must exclude it from the emitted schema."""
        pytest.importorskip("mcp.server.fastmcp")
        from mcp.server.fastmcp.utilities.context_injection import find_context_parameter
        from mcp.server.fastmcp.utilities.func_metadata import func_metadata

        def get_handler(name):
            async def handler(**kwargs):
                return {}
            return handler

        def session_extractor(ctx):
            return "sid"

        schema = {
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        }
        wrapper = create_typed_wrapper(
            "session_tool", schema, get_handler,
            inject_session=True, session_extractor=session_extractor,
        )
        ctx_kwarg = find_context_parameter(wrapper)
        assert ctx_kwarg == "ctx"
        meta = func_metadata(wrapper, skip_names=[ctx_kwarg])
        fields = set(meta.arg_model.model_fields.keys())
        assert "ctx" not in fields
        assert "query" in fields
        assert "limit" in fields

    def test_simple_wrapper_has_no_ctx(self):
        """Sanity: wrapper without session injection carries no ctx at all."""
        def get_handler(name):
            async def handler(**kwargs):
                return {}
            return handler

        schema = {"properties": {"q": {"type": "string"}}, "required": []}
        wrapper = create_typed_wrapper("plain", schema, get_handler, inject_session=False)
        assert "ctx" not in inspect.signature(wrapper).parameters
        assert "ctx" not in wrapper.__annotations__


class TestExtraArgumentPassthrough:

    def test_fastmcp_validation_preserves_process_agent_update_s22_envelope(self):
        """FastMCP must not drop internal S22 fields before dispatch middleware."""
        call_log = []

        def get_handler(name):
            async def handler(**kwargs):
                call_log.append(kwargs)
                return {"ok": True}
            return handler

        def session_extractor(ctx):
            return "session-id"

        # Mirrors the public process_agent_update shape: only the agent-knowable
        # provenance fields are named schema properties. Harness/server-known
        # fields arrive as extra top-level MCP arguments from internal callers.
        schema = {
            "properties": {
                "response_text": {"type": "string"},
                "comparison_key": {"type": "string"},
                "task_label": {"type": "string"},
                "task_outcome": {"type": "string"},
                "memory_context": {"type": "string"},
            },
            "required": [],
        }
        wrapper = create_typed_wrapper(
            "process_agent_update",
            schema,
            get_handler,
            inject_session=True,
            session_extractor=session_extractor,
        )
        tool = Tool.from_function(wrapper, structured_output=False)
        enable_extra_argument_passthrough(tool)

        sent = {
            "response_text": "round-trip check",
            "harness_type": "r6_dogfood",
            "model": "test-model",
            "tool_surface": "hermes-wrapper",
            "memory_context": "memory+kg+transcript",
            "comparison_key": "hermes-wrapper-rt-test",
            "task_label": "Hermes MCP wrapper round-trip",
            "task_outcome": "test_passed",
            "verification_source": "agent_reported_tool_result",
            "governance_mode": "governed",
            "locus": "in_process_mcp_wrapper",
        }

        asyncio.run(tool.run(sent))

        assert call_log
        for key, value in sent.items():
            assert call_log[0][key] == value
