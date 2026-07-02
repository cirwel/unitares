"""#1322: validation errors must enumerate every violation in one pass.

Pydantic v2 already reports all violations in a single ValidationError;
`_format_pydantic_error` dispatched on errors[0]["type"], so a mixed error
set (missing + extra_forbidden) surfaced one class per round-trip — the
2026-07-01 dogfood F5 payload took two failed calls to learn the
recent_tool_results contract.

Pinned behavior:
  - mixed error sets render the general formatter listing ALL violations,
    each with its type, in both the message and details.errors
  - homogeneous specialized shapes (multi-missing, single literal, single
    parsing) are preserved
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.mcp_handlers.middleware.params_step import _format_pydantic_error
from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams


def _payload(resp) -> dict:
    return json.loads(resp.text)


def _validate_error(args) -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        ProcessAgentUpdateParams.model_validate(args)
    return exc_info.value


class TestMixedViolationsEnumerated:
    def test_dogfood_f5_payload_reports_all_four_violations(self):
        """The exact F5 shape: natural fields + missing summary = 4 errors, one pass."""
        err = _validate_error({
            "response_text": "x", "complexity": 0.5, "confidence": 0.9,
            "recent_tool_results": [{
                "tool": "bash",
                "command": "git push --force",
                "result": "ok",
                "status": "success",
            }],
        })
        payload = _payload(_format_pydantic_error(err, "process_agent_update"))

        errors = payload.get("errors") or []
        fields = {e["field"] for e in errors}
        assert "recent_tool_results.0.summary" in fields
        assert "recent_tool_results.0.command" in fields
        assert "recent_tool_results.0.result" in fields
        assert "recent_tool_results.0.status" in fields

        types = {e.get("type") for e in errors}
        assert types == {"missing", "extra_forbidden"}

        # The human-readable message names every violated field too.
        message = payload.get("error") or ""
        for field in fields:
            assert field in message, f"{field} missing from message: {message}"

    def test_structured_errors_carry_full_list_beyond_message_cap(self):
        """The human message is capped (10 lines here, plus the global
        MAX_ERROR_MESSAGE_LENGTH char cap in _sanitize_error_message);
        the structured errors list always carries everything."""
        # 12 evidence items each missing `summary` + carrying one extra field
        # → 24 violations.
        err = _validate_error({
            "response_text": "x", "complexity": 0.5, "confidence": 0.9,
            "recent_tool_results": [
                {"tool": f"t{i}", "bogus": "x"} for i in range(12)
            ],
        })
        payload = _payload(_format_pydantic_error(err, "process_agent_update"))
        errors = payload.get("errors") or []
        assert len(errors) == 24
        types = {e.get("type") for e in errors}
        assert types == {"missing", "extra_forbidden"}


class TestHomogeneousShapesPreserved:
    def test_multi_missing_keeps_missing_parameter_shape(self):
        # Two evidence items each missing required `summary` — a homogeneous
        # all-missing set keeps the specialized MISSING_PARAMETER shape.
        err = _validate_error({
            "response_text": "x", "complexity": 0.5, "confidence": 0.9,
            "recent_tool_results": [{"tool": "a"}, {"tool": "b"}],
        })
        payload = _payload(_format_pydantic_error(err, "process_agent_update"))
        assert payload.get("error_code") == "MISSING_PARAMETER"
        missing = payload.get("missing_parameters") or (
            (payload.get("details") or {}).get("missing_parameters")
        ) or []
        assert "recent_tool_results.0.summary" in missing
        assert "recent_tool_results.1.summary" in missing

    def test_single_extra_field_enumerated_via_general_shape(self):
        err = _validate_error({
            "response_text": "x", "complexity": 0.5, "confidence": 0.9,
            "recent_tool_results": [{
                "tool": "bash", "summary": "ok", "bogus": "x",
            }],
        })
        payload = _payload(_format_pydantic_error(err, "process_agent_update"))
        errors = payload.get("errors") or []
        assert len(errors) == 1
        assert errors[0]["field"] == "recent_tool_results.0.bogus"
        assert errors[0]["type"] == "extra_forbidden"
