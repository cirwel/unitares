"""Tests for alias-layer parameter normalization (friendly check-in aliases).

Canonical process_agent_update stays strict 0-1; the friendly aliases
(checkin/log/update/sync_state) accept named levels and explicit
{'value', 'scale'} objects, reject ambiguous bare numerics, and disclose
every transform via normalized_parameters.
"""

import json
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.support.param_normalization import (
    NAMED_LEVELS,
    ParamNormalizationError,
    normalize_unit_interval,
)


@pytest.fixture
def normalize():
    return normalize_unit_interval("complexity")


# ============================================================================
# Pass-through (no normalization, no record)
# ============================================================================

class TestPassThrough:

    def test_absent(self, normalize):
        args = {"response_text": "work"}
        assert normalize(args) == {}
        assert "complexity" not in args

    def test_none(self, normalize):
        args = {"complexity": None}
        assert normalize(args) == {}
        assert args["complexity"] is None

    @pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 1.0])
    def test_in_range_numeric(self, normalize, value):
        args = {"complexity": value}
        assert normalize(args) == {}
        assert args["complexity"] == value

    def test_in_range_numeric_string_left_to_schema(self, normalize):
        args = {"complexity": "0.7"}
        assert normalize(args) == {}
        assert args["complexity"] == "0.7"


# ============================================================================
# Named levels
# ============================================================================

class TestNamedLevels:

    def test_medium(self, normalize):
        args = {"complexity": "medium"}
        records = normalize(args)
        assert args["complexity"] == 0.5
        assert records == {
            "complexity": {"from": "medium", "to": 0.5, "interpretation": "named_level"}
        }

    @pytest.mark.parametrize("raw,expected", [
        ("trivial", 0.1), ("minimal", 0.1),
        ("low", 0.3), ("simple", 0.3),
        ("moderate", 0.5),
        ("high", 0.7), ("complex", 0.7),
        ("very_high", 0.9), ("critical", 0.9),
    ])
    def test_all_levels(self, normalize, raw, expected):
        args = {"complexity": raw}
        normalize(args)
        assert args["complexity"] == expected

    @pytest.mark.parametrize("raw", ["Medium", " MEDIUM ", "very high", "very-high"])
    def test_case_and_separator_insensitive(self, normalize, raw):
        args = {"complexity": raw}
        records = normalize(args)
        assert args["complexity"] in (0.5, 0.9)
        assert records["complexity"]["from"] == raw

    def test_levels_are_unit_interval(self):
        for level, value in NAMED_LEVELS.items():
            assert 0.0 <= value <= 1.0, level


# ============================================================================
# Explicit scale objects
# ============================================================================

class TestExplicitScale:

    def test_scale_10(self, normalize):
        args = {"complexity": {"value": 5, "scale": 10}}
        records = normalize(args)
        assert args["complexity"] == 0.5
        assert records == {
            "complexity": {
                "from": {"value": 5, "scale": 10},
                "to": 0.5,
                "interpretation": "explicit_scale",
            }
        }

    def test_scale_5(self, normalize):
        args = {"complexity": {"value": 4, "scale": 5}}
        normalize(args)
        assert args["complexity"] == 0.8

    def test_scale_100(self, normalize):
        args = {"complexity": {"value": 73, "scale": 100}}
        normalize(args)
        assert args["complexity"] == 0.73

    def test_boundary_values(self, normalize):
        args = {"complexity": {"value": 0, "scale": 10}}
        normalize(args)
        assert args["complexity"] == 0.0
        args = {"complexity": {"value": 10, "scale": 10}}
        normalize(args)
        assert args["complexity"] == 1.0

    @pytest.mark.parametrize("bad", [
        {"value": 11, "scale": 10},      # value above scale
        {"value": -1, "scale": 10},      # negative value
        {"value": 5, "scale": 0.5},      # degenerate scale
        {"value": 5},                    # missing scale
        {"scale": 10},                   # missing value
        {"value": 5, "scale": 10, "x": 1},  # unexpected key
        {"value": "5", "scale": 10},     # non-numeric value
        {"value": True, "scale": 10},    # bool is not a number here
    ])
    def test_invalid_scale_objects_reject(self, normalize, bad):
        with pytest.raises(ParamNormalizationError):
            normalize({"complexity": bad})


# ============================================================================
# Ambiguous / invalid values reject — never silently rescaled
# ============================================================================

class TestAmbiguousRejection:

    @pytest.mark.parametrize("value", [5, 1.5, 10, 11, -0.1, "5", "10"])
    def test_bare_out_of_range_rejects_as_ambiguous(self, normalize, value):
        with pytest.raises(ParamNormalizationError) as exc:
            normalize({"complexity": value})
        assert "scale" in str(exc.value)
        assert exc.value.parameter == "complexity"
        assert exc.value.provided == value

    def test_unknown_word_rejects_listing_levels(self, normalize):
        with pytest.raises(ParamNormalizationError) as exc:
            normalize({"complexity": "huge"})
        assert "named level" in str(exc.value)

    @pytest.mark.parametrize("value", [True, False, [0.5]])
    def test_unsupported_types_reject(self, normalize, value):
        with pytest.raises(ParamNormalizationError):
            normalize({"complexity": value})


# ============================================================================
# resolve_alias middleware integration
# ============================================================================

class TestResolveAliasIntegration:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("alias", ["checkin", "log", "update", "sync_state"])
    async def test_named_level_normalized_and_recorded(self, alias):
        from src.mcp_handlers.middleware import DispatchContext
        from src.mcp_handlers.middleware.params_step import resolve_alias

        ctx = DispatchContext()
        args = {"complexity": "medium", "response_text": "work"}
        name, out_args, out_ctx = await resolve_alias(alias, args, ctx)

        assert name == "process_agent_update"
        assert out_args["complexity"] == 0.5
        assert out_ctx.normalized_parameters == {
            "complexity": {"from": "medium", "to": 0.5, "interpretation": "named_level"}
        }

    @pytest.mark.asyncio
    async def test_explicit_scale_normalized(self):
        from src.mcp_handlers.middleware import DispatchContext
        from src.mcp_handlers.middleware.params_step import resolve_alias

        ctx = DispatchContext()
        args = {"complexity": {"value": 5, "scale": 10}}
        name, out_args, out_ctx = await resolve_alias("sync_state", args, ctx)

        assert out_args["complexity"] == 0.5
        assert out_ctx.normalized_parameters["complexity"]["interpretation"] == "explicit_scale"

    @pytest.mark.asyncio
    async def test_bare_5_short_circuits_with_ambiguous_error(self):
        from src.mcp_handlers.middleware import DispatchContext
        from src.mcp_handlers.middleware.params_step import resolve_alias

        ctx = DispatchContext()
        result = await resolve_alias("sync_state", {"complexity": 5}, ctx)

        assert isinstance(result, list)
        payload = json.loads(result[0].text)
        assert payload["success"] is False
        assert payload["error_code"] == "PARAMETER_ERROR"
        assert payload["error_type"] == "ambiguous_parameter_value"
        assert payload["parameter"] == "complexity"
        assert "scale" in payload["error"]

    @pytest.mark.asyncio
    async def test_in_range_value_passes_with_no_record(self):
        from src.mcp_handlers.middleware import DispatchContext
        from src.mcp_handlers.middleware.params_step import resolve_alias

        ctx = DispatchContext()
        name, out_args, out_ctx = await resolve_alias("checkin", {"complexity": 0.5}, ctx)

        assert name == "process_agent_update"
        assert out_args["complexity"] == 0.5
        assert out_ctx.normalized_parameters is None

    @pytest.mark.asyncio
    async def test_canonical_name_is_not_normalized(self):
        """The canonical tool gets no alias-layer tolerance — schema rejects."""
        from src.mcp_handlers.middleware import DispatchContext
        from src.mcp_handlers.middleware.params_step import resolve_alias

        ctx = DispatchContext()
        args = {"complexity": "medium"}
        name, out_args, _ = await resolve_alias("process_agent_update", args, ctx)

        assert name == "process_agent_update"
        assert out_args["complexity"] == "medium"  # untouched; validation rejects later


# ============================================================================
# Envelope disclosure
# ============================================================================

class TestEnvelopeDisclosure:

    def test_normalized_parameters_injected_into_json_payload(self):
        from mcp.types import TextContent
        from src.services.tool_dispatch_service import _disclose_normalized_parameters

        result = [TextContent(type="text", text=json.dumps({"success": True}))]
        records = {"complexity": {"from": "medium", "to": 0.5, "interpretation": "named_level"}}
        out = _disclose_normalized_parameters(result, records)

        payload = json.loads(out[0].text)
        assert payload["success"] is True
        assert payload["normalized_parameters"] == records

    def test_non_json_payload_untouched(self):
        from mcp.types import TextContent
        from src.services.tool_dispatch_service import _disclose_normalized_parameters

        result = [TextContent(type="text", text="plain text")]
        out = _disclose_normalized_parameters(result, {"complexity": {}})
        assert out[0].text == "plain text"

    def test_empty_result_untouched(self):
        from src.services.tool_dispatch_service import _disclose_normalized_parameters

        assert _disclose_normalized_parameters(None, {"x": {}}) is None
        assert _disclose_normalized_parameters([], {"x": {}}) == []
