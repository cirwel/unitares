"""
Comprehensive tests for src/mcp_handlers/utils.py

Covers:
- _infer_error_code_and_category: pattern-based error code inference
- compute_agent_signature: priority resolution (explicit > context > session)
- check_agent_can_operate: circuit breaker enforcement
- error_response / success_response: JSON TextContent creation
- format_metrics_report / format_metrics_text: metrics formatting
- _sanitize_error_message: security sanitization
- _make_json_serializable: type coercion for JSON
- require_agent_id / require_registered_agent: agent resolution
- require_argument: argument validation
- verify_agent_ownership: ownership checks
- generate_actionable_feedback: context-aware feedback
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, date
from enum import Enum
from types import SimpleNamespace

from src.mcp_handlers.utils import (
    _infer_error_code_and_category,
    compute_agent_signature,
    check_agent_can_operate,
    error_response,
    success_response,
    format_metrics_report,
    format_metrics_text,
    _sanitize_error_message,
    _make_json_serializable,
    require_argument,
    require_agent_id,
    require_registered_agent,
    verify_agent_ownership,
    generate_actionable_feedback,
)
from mcp.types import TextContent


def _parse_tc(tc):
    assert isinstance(tc, TextContent)
    return json.loads(tc.text)

def _mock_server(meta=None):
    s = MagicMock()
    s.agent_metadata = meta or {}
    return s

def _meta(status="active", label=None, structured_id=None,
          display_name=None, paused_at=None, agent_uuid=None, tags=None,
          public_agent_id=None):
    return SimpleNamespace(status=status, label=label, structured_id=structured_id,
                           display_name=display_name, paused_at=paused_at,
                           agent_uuid=agent_uuid, tags=tags or [],
                           public_agent_id=public_agent_id)


class TestInferErrorCodeAndCategory:
    def test_not_found(self):
        assert _infer_error_code_and_category("Agent not found") == ("NOT_FOUND", "validation_error")
    def test_does_not_exist(self):
        assert _infer_error_code_and_category("does not exist") == ("NOT_FOUND", "validation_error")
    def test_doesnt_exist(self):
        assert _infer_error_code_and_category("doesn't exist") == ("NOT_FOUND", "validation_error")
    def test_missing_required(self):
        assert _infer_error_code_and_category("Missing required param") == ("MISSING_REQUIRED", "validation_error")
    def test_required_parameter(self):
        assert _infer_error_code_and_category("required parameter x") == ("MISSING_REQUIRED", "validation_error")
    def test_must_provide(self):
        assert _infer_error_code_and_category("must provide id") == ("MISSING_REQUIRED", "validation_error")
    def test_invalid(self):
        assert _infer_error_code_and_category("Invalid format") == ("INVALID_PARAM", "validation_error")
    def test_must_be(self):
        assert _infer_error_code_and_category("must be positive") == ("INVALID_PARAM", "validation_error")
    def test_should_be(self):
        assert _infer_error_code_and_category("should be int") == ("INVALID_PARAM", "validation_error")
    def test_already_exists(self):
        assert _infer_error_code_and_category("already exists") == ("ALREADY_EXISTS", "validation_error")
    def test_duplicate(self):
        assert _infer_error_code_and_category("Duplicate entry") == ("ALREADY_EXISTS", "validation_error")
    def test_too_long(self):
        assert _infer_error_code_and_category("too long") == ("VALUE_TOO_LARGE", "validation_error")
    def test_exceeds_maximum(self):
        assert _infer_error_code_and_category("exceeds maximum") == ("VALUE_TOO_LARGE", "validation_error")
    def test_too_large(self):
        assert _infer_error_code_and_category("too large") == ("VALUE_TOO_LARGE", "validation_error")
    def test_too_short(self):
        assert _infer_error_code_and_category("too short") == ("VALUE_TOO_SMALL", "validation_error")
    def test_too_small(self):
        assert _infer_error_code_and_category("too small") == ("VALUE_TOO_SMALL", "validation_error")
    def test_minimum(self):
        assert _infer_error_code_and_category("below minimum") == ("VALUE_TOO_SMALL", "validation_error")
    def test_empty_value(self):
        assert _infer_error_code_and_category("cannot be empty") == ("EMPTY_VALUE", "validation_error")
    def test_permission_denied(self):
        assert _infer_error_code_and_category("Permission denied") == ("PERMISSION_DENIED", "auth_error")
    def test_not_authorized(self):
        assert _infer_error_code_and_category("not authorized") == ("PERMISSION_DENIED", "auth_error")
    def test_forbidden(self):
        assert _infer_error_code_and_category("forbidden") == ("PERMISSION_DENIED", "auth_error")
    def test_access_denied(self):
        assert _infer_error_code_and_category("access denied") == ("PERMISSION_DENIED", "auth_error")
    def test_api_key(self):
        assert _infer_error_code_and_category("api key expired") == ("API_KEY_ERROR", "auth_error")
    def test_apikey(self):
        assert _infer_error_code_and_category("apikey expired") == ("API_KEY_ERROR", "auth_error")
    def test_session(self):
        assert _infer_error_code_and_category("Session expired") == ("SESSION_ERROR", "auth_error")
    def test_identity_not_resolved(self):
        assert _infer_error_code_and_category("identity not resolved") == ("SESSION_ERROR", "auth_error")
    def test_paused(self):
        assert _infer_error_code_and_category("is paused") == ("AGENT_PAUSED", "state_error")
    def test_archived(self):
        assert _infer_error_code_and_category("is archived") == ("AGENT_ARCHIVED", "state_error")
    def test_deleted(self):
        assert _infer_error_code_and_category("is deleted") == ("AGENT_DELETED", "state_error")
    def test_locked(self):
        assert _infer_error_code_and_category("resource locked") == ("RESOURCE_LOCKED", "state_error")
    def test_already_locked(self):
        assert _infer_error_code_and_category("already locked") == ("RESOURCE_LOCKED", "state_error")
    def test_timeout(self):
        assert _infer_error_code_and_category("timeout") == ("TIMEOUT", "system_error")
    def test_timed_out(self):
        assert _infer_error_code_and_category("timed out") == ("TIMEOUT", "system_error")
    def test_connection(self):
        assert _infer_error_code_and_category("Connection refused") == ("CONNECTION_ERROR", "system_error")
    def test_connect(self):
        assert _infer_error_code_and_category("could not connect") == ("CONNECTION_ERROR", "system_error")
    def test_database(self):
        assert _infer_error_code_and_category("Database error") == ("DATABASE_ERROR", "system_error")
    def test_postgres(self):
        assert _infer_error_code_and_category("Postgres pool") == ("DATABASE_ERROR", "system_error")
    def test_db_error(self):
        assert _infer_error_code_and_category("db error") == ("DATABASE_ERROR", "system_error")
    def test_failed_to(self):
        assert _infer_error_code_and_category("Failed to do X") == ("OPERATION_FAILED", "system_error")
    def test_could_not(self):
        assert _infer_error_code_and_category("Could not parse") == ("OPERATION_FAILED", "system_error")
    def test_unable_to(self):
        assert _infer_error_code_and_category("Unable to proceed") == ("OPERATION_FAILED", "system_error")
    def test_no_match(self):
        assert _infer_error_code_and_category("Everything is fine") == (None, None)
    def test_empty_string(self):
        assert _infer_error_code_and_category("") == (None, None)
    def test_case_insensitive(self):
        assert _infer_error_code_and_category("AGENT NOT FOUND") == ("NOT_FOUND", "validation_error")
    def test_first_match_wins(self):
        code, _ = _infer_error_code_and_category("not found, failed to get")
        assert code == "NOT_FOUND"


class TestComputeAgentSignature:
    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_explicit_priority(self, mock_srv, mock_ctx):
        mock_ctx.return_value = "ctx-id"
        mock_srv.return_value = _mock_server({"exp-id": _meta(label="Exp")})
        sig = compute_agent_signature(agent_id="exp-id")
        assert sig["uuid"] == "exp-id"
        assert sig.get("display_name") == "Exp"

    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_context_fallback(self, mock_srv, mock_ctx):
        mock_ctx.return_value = "ctx-456"
        mock_srv.return_value = _mock_server({"ctx-456": _meta(label="Ctx", structured_id="S1")})
        sig = compute_agent_signature()
        assert sig["uuid"] == "ctx-456"
        # P1.3: agent_id carries the STRUCTURED handle, not the display label;
        # the label lives in display_name.
        assert sig["agent_id"] == "S1"
        assert sig["structured_agent_id"] == "S1"
        assert sig["display_name"] == "Ctx"
        assert sig["identity_context"]["schema"] == "s22.identity_response.v1"
        assert sig["identity_context"]["public_handle"]["agent_id"] == "S1"
        assert sig["identity_context"]["label"]["display_name"] == "Ctx"

    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_no_id_returns_none(self, mock_srv, mock_ctx):
        mock_ctx.return_value = None
        mock_srv.return_value = _mock_server()
        assert compute_agent_signature() == {"uuid": None}

    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_not_in_metadata(self, mock_srv, mock_ctx):
        mock_ctx.return_value = "orphan"
        mock_srv.return_value = _mock_server()
        sig = compute_agent_signature()
        assert sig["uuid"] == "orphan"
        assert "display_name" not in sig

    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_structured_id_only(self, mock_srv, mock_ctx):
        mock_ctx.return_value = "sid"
        mock_srv.return_value = _mock_server({"sid": _meta(structured_id="G1")})
        sig = compute_agent_signature()
        assert sig["agent_id"] == "G1"
        assert "display_name" not in sig

    @patch("src.mcp_handlers.context.get_context_agent_id", side_effect=Exception("boom"))
    def test_exception_safe(self, mock_ctx):
        assert compute_agent_signature(agent_id="x") == {"uuid": None}

    # ---- label_source: dual-label visibility (identity-honesty axiom) ----

    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_label_source_claimed_when_label_differs_from_auto_ids(self, mock_srv, mock_ctx):
        """Agent-chosen label (differs from public_agent_id/structured_id) → claimed."""
        mock_ctx.return_value = "uuid-1"
        mock_srv.return_value = _mock_server({
            "uuid-1": _meta(label="hikewa", public_agent_id="Claude_Code_20260417", structured_id="mcp_20260417"),
        })
        sig = compute_agent_signature()
        # P1.3: the structured handle is surfaced in agent_id; the claimed
        # label is reported via display_name + label_source, not agent_id.
        assert sig["agent_id"] == "Claude_Code_20260417"
        assert sig["display_name"] == "hikewa"
        assert sig["label_source"] == "claimed"
        assert sig["identity_context"]["public_handle"]["agent_id"] == "Claude_Code_20260417"
        assert sig["identity_context"]["label"]["display_name"] == "hikewa"

    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_agent_id_carries_structured_handle_not_label(self, mock_srv, mock_ctx):
        """P1.3 regression: when both a display label and a structured handle
        exist, agent_id must be the structured handle (never the bare label),
        and the label must be reachable only via display_name."""
        mock_ctx.return_value = "uuid-p13"
        mock_srv.return_value = _mock_server({
            "uuid-p13": _meta(label="claude-opus48-dogfood",
                              public_agent_id="Claude_Opus_4_8_20260613"),
        })
        sig = compute_agent_signature()
        assert sig["agent_id"] == "Claude_Opus_4_8_20260613"
        assert sig["agent_id"] != "claude-opus48-dogfood"
        assert sig["display_name"] == "claude-opus48-dogfood"
        assert sig["structured_agent_id"] == "Claude_Opus_4_8_20260613"
        assert sig["identity_context"]["schema"] == "s22.identity_response.v1"
        assert sig["identity_context"]["agent_id_is"] == "public_structured_handle"
        assert sig["identity_context"]["label"]["display_name"] == "claude-opus48-dogfood"

    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_label_source_auto_when_label_matches_public_agent_id(self, mock_srv, mock_ctx):
        """Label equals the auto-derived public_agent_id → auto."""
        mock_ctx.return_value = "uuid-2"
        mock_srv.return_value = _mock_server({
            "uuid-2": _meta(label="Claude_Code_20260417", public_agent_id="Claude_Code_20260417"),
        })
        sig = compute_agent_signature()
        assert sig["label_source"] == "auto"

    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_label_source_auto_when_only_structured_id(self, mock_srv, mock_ctx):
        """No label, only structured_id → auto."""
        mock_ctx.return_value = "uuid-3"
        mock_srv.return_value = _mock_server({"uuid-3": _meta(structured_id="mcp_20260417")})
        sig = compute_agent_signature()
        assert sig["label_source"] == "auto"

    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_label_source_uuid_when_nothing_else(self, mock_srv, mock_ctx):
        """No label, no auto IDs → display is UUID, label_source reflects that."""
        mock_ctx.return_value = "uuid-4"
        mock_srv.return_value = _mock_server({"uuid-4": _meta()})
        sig = compute_agent_signature()
        assert sig["label_source"] == "uuid"


class TestCheckAgentCanOperate:
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_unknown_allowed(self, mock_srv):
        mock_srv.return_value = _mock_server()
        assert check_agent_can_operate("unknown") is None

    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_active_allowed(self, mock_srv):
        mock_srv.return_value = _mock_server({"a": _meta(status="active")})
        assert check_agent_can_operate("a") is None

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_paused_blocked(self, mock_srv, mock_sig):
        from datetime import datetime as _dt
        # Fresh paused_at — pause TTL auto-expires stale ones (>72h default)
        mock_srv.return_value = _mock_server({"p": _meta(status="paused", paused_at=_dt.now().isoformat())})
        r = check_agent_can_operate("p")
        assert r is not None
        d = _parse_tc(r)
        assert d["success"] is False
        assert d["error_code"] == "AGENT_PAUSED"
        assert "self_recovery" in d["recovery"]["action"]

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_archived_blocked(self, mock_srv, mock_sig):
        mock_srv.return_value = _mock_server({"a": _meta(status="archived")})
        d = _parse_tc(check_agent_can_operate("a"))
        assert d["error_code"] == "AGENT_ARCHIVED"

    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_other_status_allowed(self, mock_srv):
        mock_srv.return_value = _mock_server({"w": _meta(status="waiting_input")})
        assert check_agent_can_operate("w") is None


class TestErrorResponse:
    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_basic(self, mock_sig):
        r = error_response("Something went wrong")
        assert isinstance(r, TextContent)
        d = _parse_tc(r)
        assert d["success"] is False
        assert "went wrong" in d["error"]
        assert "server_time" in d

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_explicit_code_category(self, mock_sig):
        d = _parse_tc(error_response("x", error_code="C", error_category="validation_error"))
        assert d["error_code"] == "C"
        assert d["error_category"] == "validation_error"

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_auto_inferred(self, mock_sig):
        d = _parse_tc(error_response("Resource not found"))
        assert d["error_code"] == "NOT_FOUND"

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_explicit_overrides(self, mock_sig):
        d = _parse_tc(error_response("not found", error_code="MY", error_category="system_error"))
        assert d["error_code"] == "MY"
        assert d["error_category"] == "system_error"

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_partial_explicit(self, mock_sig):
        d = _parse_tc(error_response("Agent not found", error_code="MY"))
        assert d["error_code"] == "MY"
        assert d["error_category"] == "validation_error"

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_details_merged(self, mock_sig):
        d = _parse_tc(error_response("x", details={"agent_id": "abc", "count": 42}))
        assert d["agent_id"] == "abc" and d["count"] == 42

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_recovery(self, mock_sig):
        d = _parse_tc(error_response("x", recovery={"action": "retry"}))
        assert d["recovery"]["action"] == "retry"

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_context(self, mock_sig):
        d = _parse_tc(error_response("x", context={"op": "search"}))
        assert d["context"]["op"] == "search"

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_no_code_for_generic(self, mock_sig):
        d = _parse_tc(error_response("All systems nominal"))
        assert "error_code" not in d

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_sanitize_details(self, mock_sig):
        d = _parse_tc(error_response("x", details={"p": "/Users/s/h/d/m.py crash"}))
        assert "/Users/s" not in d.get("p", "")


class TestSuccessResponse:
    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": "u1"})
    def test_basic(self, ms):
        r = success_response({"msg": "hi"})
        d = _parse_tc(r[0])
        assert d["success"] is True and d["msg"] == "hi"
        assert d["agent_signature"]["uuid"] == "u1"
        assert "caller_agent_id" not in d

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": "u1"})
    def test_lite_omits_sig(self, ms):
        d = _parse_tc(success_response({"x": 1}, arguments={"lite_response": True})[0])
        assert "agent_signature" not in d

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_no_sig_when_unbound(self, ms):
        d = _parse_tc(success_response({"x": 1})[0])
        assert "caller_agent_id" not in d

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": "u1"})
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="u1")
    def test_param_coercions(self, mc, ms):
        c = [{"param": "c", "from": "0.8", "to": 0.8}]
        d = _parse_tc(success_response({"x": 1}, arguments={"_param_coercions": c})[0])
        assert d["_param_coercions"]["applied"] == c

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": "u1"})
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="u1")
    def test_coercions_hidden_lite(self, mc, ms):
        d = _parse_tc(success_response({"x": 1}, arguments={"lite_response": True, "_param_coercions": [{"p": "x"}]})[0])
        assert "_param_coercions" not in d

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    def test_non_serializable(self, mc, ms):
        d = _parse_tc(success_response({"ts": datetime(2026, 1, 15, 12, 0), "d": date(2026, 1, 15)})[0])
        assert d["ts"] == "2026-01-15T12:00:00" and d["d"] == "2026-01-15"

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": "u1"})
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="u1")
    def test_explicit_agent_id_forwarded(self, mc, ms):
        success_response({"x": 1}, agent_id="my-a")
        ms.assert_called_once_with(agent_id="my-a", arguments=None)


class TestFormatMetricsReport:
    def test_basic(self):
        r = format_metrics_report({"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}, "a1")
        assert r["agent_id"] == "a1" and "timestamp" in r
        assert r["eisv"]["E"] == 0.8

    def test_no_timestamp(self):
        assert "timestamp" not in format_metrics_report({"E": 0.5}, "a1", include_timestamp=False)

    def test_no_context(self):
        assert "eisv" not in format_metrics_report({"E": 0.5, "I": 0.6, "S": 0.1, "V": 0.0}, "a1", include_context=False)

    def test_health_status(self):
        assert format_metrics_report({"health_status": "healthy"}, "a1")["health_status"] == "healthy"

    def test_text_style(self):
        r = format_metrics_report({"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05}, "a1", format_style="text")
        assert isinstance(r, str) and "Agent: a1" in r

    def test_agent_id_override(self):
        assert format_metrics_report({"agent_id": "old"}, "new")["agent_id"] == "new"

    def test_partial_eisv(self):
        assert format_metrics_report({"E": 0.7, "S": 0.2}, "a1")["eisv"] == {"E": 0.7, "S": 0.2}

    def test_no_eisv(self):
        assert "eisv" not in format_metrics_report({"coherence": 0.95}, "a1")


class TestFormatMetricsText:
    def test_full(self):
        t = format_metrics_text({"agent_id": "t", "timestamp": "T", "health_status": "ok",
                                  "eisv": {"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05},
                                  "coherence": 0.95, "risk_score": 0.2})
        assert "Agent: t" in t and "coherence: 0.950" in t

    def test_flat_eisv(self):
        assert "EISV: E=0.500" in format_metrics_text({"agent_id": "a", "E": 0.5, "I": 0.6, "S": 0.3, "V": 0.0})

    def test_unknown_agent(self):
        assert "Agent: unknown" in format_metrics_text({})

    def test_non_float_metric(self):
        assert "verdict: safe" in format_metrics_text({"agent_id": "a", "verdict": "safe"})

    def test_float_formatted(self):
        assert "phi: 1.235" in format_metrics_text({"agent_id": "a", "phi": 1.23456})


class TestSanitizeErrorMessage:
    def test_removes_path(self):
        m = _sanitize_error_message("Error /Users/c/p/g/src/module.py")
        assert "/Users/c" not in m and "module.py" in m

    def test_removes_line_number(self):
        assert "line 42" not in _sanitize_error_message("at line 42, in process")

    def test_preserves_error_codes(self):
        assert "AGENT_NOT_FOUND" in _sanitize_error_message("AGENT_NOT_FOUND: x")

    def test_non_string(self):
        assert _sanitize_error_message(12345) == "12345"

    def test_strips_internal_paths(self):
        m = _sanitize_error_message("src.mcp_handlers.utils.compute failed")
        assert "src.mcp_handlers.utils." not in m

    def test_cleans_spaces(self):
        assert "  " not in _sanitize_error_message("too   many    spaces")

    def test_truncates_long(self):
        assert len(_sanitize_error_message("A" * 1000)) <= 503

    def test_traceback(self):
        assert "most recent call last" not in _sanitize_error_message(
            "Traceback (most recent call last):\n  File \"/f.py\"\nValueError: bad")

    def test_empty(self):
        assert _sanitize_error_message("") == ""


class TestMakeJsonSerializable:
    def test_none(self):
        assert _make_json_serializable(None) is None

    def test_primitives(self):
        assert _make_json_serializable("hi") == "hi"
        assert _make_json_serializable(42) == 42
        assert _make_json_serializable(True) is True

    def test_datetime(self):
        assert _make_json_serializable(datetime(2026, 1, 15, 12, 30)) == "2026-01-15T12:30:00"

    def test_date(self):
        assert _make_json_serializable(date(2026, 1, 15)) == "2026-01-15"

    def test_enum(self):
        class C(Enum):
            R = "red"
        assert _make_json_serializable(C.R) == "red"

    def test_dict_recursive(self):
        r = _make_json_serializable({"k": datetime(2026, 1, 1), "n": {"v": date(2026, 2, 2)}})
        assert r["k"] == "2026-01-01T00:00:00" and r["n"]["v"] == "2026-02-02"

    def test_list_recursive(self):
        assert _make_json_serializable([datetime(2026, 1, 1), "hi"]) == ["2026-01-01T00:00:00", "hi"]

    def test_tuple(self):
        assert _make_json_serializable((1, "two")) == [1, "two"]

    def test_set(self):
        r = _make_json_serializable({1, 2, 3})
        assert isinstance(r, list) and sorted(r) == [1, 2, 3]

    def test_large_list(self):
        r = _make_json_serializable(list(range(200)))
        assert len(r) == 101 and "100 more" in r[-1]

    def test_large_set(self):
        assert len(_make_json_serializable(set(range(200)))) == 101

    def test_unknown_type(self):
        class X:
            def __str__(self): return "x_obj"
        assert _make_json_serializable(X()) == "x_obj"

    def test_numpy(self):
        import numpy as np
        assert _make_json_serializable(np.float64(3.14)) == 3.14
        assert isinstance(_make_json_serializable(np.float64(3.14)), float)
        assert _make_json_serializable(np.int64(42)) == 42
        assert _make_json_serializable(np.bool_(True)) is True
        assert _make_json_serializable(np.array([1, 2])) == [1, 2]

    def test_end_to_end(self):
        class E(Enum):
            F = "foo"
        r = _make_json_serializable({"dt": datetime(2026, 6, 15), "e": E.F, "s": {10, 20}})
        p = json.loads(json.dumps(r))
        assert p["dt"] == "2026-06-15T00:00:00" and p["e"] == "foo"


class TestRequireArgument:
    def test_present(self):
        v, e = require_argument({"n": "Alice"}, "n")
        assert v == "Alice" and e is None

    def test_missing(self):
        v, e = require_argument({}, "n")
        assert v is None and isinstance(e, TextContent)

    def test_none_missing(self):
        v, e = require_argument({"n": None}, "n")
        assert v is None and e is not None

    def test_custom_msg(self):
        _, e = require_argument({}, "age", error_message="Need age")
        assert e is not None

    def test_zero_valid(self):
        v, e = require_argument({"c": 0}, "c")
        assert v == 0 and e is None

    def test_empty_str_valid(self):
        v, e = require_argument({"n": ""}, "n")
        assert v == "" and e is None

    def test_false_valid(self):
        v, e = require_argument({"f": False}, "f")
        assert v is False and e is None


class TestRequireAgentId:
    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=("test", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format", return_value=("test", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    def test_explicit(self, mc, mf, mr):
        a, e = require_agent_id({"agent_id": "test"})
        assert a == "test" and e is None

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=("ctx", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format", return_value=("ctx", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="ctx")
    def test_context_fallback(self, mc, mf, mr):
        args = {}
        a, e = require_agent_id(args)
        assert a == "ctx" and args["agent_id"] == "ctx"

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=(None, None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format")
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    def test_auto_generate(self, mc, mf, mr):
        mf.side_effect = lambda x: (x, None)
        args = {}
        require_agent_id(args)
        assert args.get("agent_id") is not None

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=("bound-uuid", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format", return_value=("bound-uuid", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="bound-uuid")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_explicit_alias_of_bound_rewritten_to_uuid(self, ms, mc, mf, mr):
        """If the explicit agent_id is a label/structured_id of the bound agent,
        rewrite it to the canonical UUID so downstream metadata lookups work."""
        s = _mock_server({"bound-uuid": _meta(label="Bot", structured_id="S1", display_name="Bot")})
        ms.return_value = s
        args = {"agent_id": "Bot"}  # alias of the bound agent
        a, e = require_agent_id(args)
        assert a == "bound-uuid", "alias should be rewritten to bound UUID"
        assert args["agent_id"] == "bound-uuid"
        assert e is None

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=("Other", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format", return_value=("Other", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="bound-uuid")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_explicit_cross_agent_not_silently_substituted(self, ms, mc, mf, mr):
        """Regression: when the explicit agent_id names a different agent than
        the session-bound one (and isn't an alias of the bound agent), the
        explicit value must be honored. Silent substitution here violated the
        identity invariant and caused cross-agent writes on agent.update calls
        to land on the caller's own record."""
        s = _mock_server({"bound-uuid": _meta(label="Caller", structured_id="S-caller", display_name="Caller")})
        ms.return_value = s
        args = {"agent_id": "Other"}  # label of a different agent
        a, e = require_agent_id(args)
        assert a == "Other", f"explicit cross-agent id must be honored, got {a!r}"
        assert args["agent_id"] == "Other"
        assert e is None


class TestRequireRegisteredAgent:
    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=("u1", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format", return_value=("u1", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="u1")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_registered(self, ms, mc, mf, mr):
        s = _mock_server({"u1": _meta(label="Bot", structured_id="S1", display_name="Bot")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        args = {"agent_id": "u1"}
        u, e = require_registered_agent(args)
        assert u == "u1" and e is None and "_agent_display" in args

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=("unk", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format", return_value=("unk", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_unregistered(self, ms, mc, mf, mr):
        s = _mock_server()
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None}):
            u, e = require_registered_agent({"agent_id": "unk"})
            assert u is None and "not registered" in _parse_tc(e)["error"]

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=("Bot", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format", return_value=("Bot", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_lookup_by_label(self, ms, mc, mf, mr):
        s = _mock_server({"real-uuid": _meta(label="Bot", structured_id="S1")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        u, e = require_registered_agent({"agent_id": "Bot"})
        assert u == "real-uuid" and e is None

    # --- S21-b §3 status-check (council pass-2 stale-positive class) ---
    # `update_identity_status` writes only PG, so the in-memory dict drifts
    # (live-verifier observed 67 active/archived inversions). The auth check
    # must gate on meta.status to refuse rows whose dict copy is stale-active.

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names",
           return_value=("11111111-1111-4111-8111-111111111111", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format",
           return_value=("11111111-1111-4111-8111-111111111111", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_archived_agent_rejected(self, ms, mc, mf, mr):
        uid = "11111111-1111-4111-8111-111111111111"
        s = _mock_server({uid: _meta(status="archived", label="Old")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature",
                   return_value={"uuid": None}):
            u, e = require_registered_agent({"agent_id": uid})
        assert u is None
        err = _parse_tc(e)["error"]
        assert "archived" in err.lower()

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names",
           return_value=("77777777-7777-4777-8777-777777777777", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format",
           return_value=("77777777-7777-4777-8777-777777777777", None))
    @patch("src.mcp_handlers.context.get_context_agent_id",
           return_value="77777777-7777-4777-8777-777777777777")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_core_identity_result_registers_agent_without_dict_entry(self, ms, mc, mf, mr):
        uid = "77777777-7777-4777-8777-777777777777"
        s = _mock_server({})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        identity_result = {
            "agent_uuid": uid,
            "agent_id": "Gpt_5_Codex_20260429",
            "public_agent_id": "Gpt_5_Codex_20260429",
            "label": "Codex_77777777",
            "core_agent_row_status": "active",
        }
        with patch(
            "src.mcp_handlers.context.get_session_context",
            return_value={"identity_result": identity_result},
        ):
            args = {"agent_id": uid}
            u, e = require_registered_agent(args)

        assert u == uid
        assert e is None
        assert args["_agent_uuid"] == uid
        assert args["agent_id"] == "Gpt_5_Codex_20260429"

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names",
           return_value=("88888888-8888-4888-8888-888888888888", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format",
           return_value=("88888888-8888-4888-8888-888888888888", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_core_agent_row_status_overrides_stale_active_dict(self, ms, mc, mf, mr):
        uid = "88888888-8888-4888-8888-888888888888"
        s = _mock_server({uid: _meta(status="active", label="Old")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        identity_result = {
            "agent_uuid": uid,
            "agent_id": uid,
            "label": "Old",
            "core_agent_row_status": "archived",
        }
        with patch(
            "src.mcp_handlers.context.get_session_context",
            return_value={"identity_result": identity_result},
        ), patch("src.mcp_handlers.support.agent_auth.compute_agent_signature",
                 return_value={"uuid": None}):
            u, e = require_registered_agent({"agent_id": uid})

        assert u is None
        assert "archived" in _parse_tc(e)["error"].lower()

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names",
           return_value=("22222222-2222-4222-8222-222222222222", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format",
           return_value=("22222222-2222-4222-8222-222222222222", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_deleted_agent_rejected(self, ms, mc, mf, mr):
        uid = "22222222-2222-4222-8222-222222222222"
        s = _mock_server({uid: _meta(status="deleted", label="Gone")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature",
                   return_value={"uuid": None}):
            u, e = require_registered_agent({"agent_id": uid})
        assert u is None
        err = _parse_tc(e)["error"]
        assert "deleted" in err.lower()

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names",
           return_value=("33333333-3333-4333-8333-333333333333", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format",
           return_value=("33333333-3333-4333-8333-333333333333", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_disabled_agent_rejected(self, ms, mc, mf, mr):
        uid = "33333333-3333-4333-8333-333333333333"
        s = _mock_server({uid: _meta(status="disabled", label="Off")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature",
                   return_value={"uuid": None}):
            u, e = require_registered_agent({"agent_id": uid})
        assert u is None
        err = _parse_tc(e)["error"]
        assert "disabled" in err.lower()

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names",
           return_value=("44444444-4444-4444-8444-444444444444", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format",
           return_value=("44444444-4444-4444-8444-444444444444", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_paused_agent_allowed(self, ms, mc, mf, mr):
        # Paused is a recoverable state (council H6 — preserve binding).
        uid = "44444444-4444-4444-8444-444444444444"
        s = _mock_server({uid: _meta(status="paused", label="Wait")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        u, e = require_registered_agent({"agent_id": uid})
        assert u == uid and e is None

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names",
           return_value=("55555555-5555-4555-8555-555555555555", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format",
           return_value=("55555555-5555-4555-8555-555555555555", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_waiting_input_agent_allowed(self, ms, mc, mf, mr):
        uid = "55555555-5555-4555-8555-555555555555"
        s = _mock_server({uid: _meta(status="waiting_input", label="Q")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        u, e = require_registered_agent({"agent_id": uid})
        assert u == uid and e is None

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names",
           return_value=("66666666-6666-4666-8666-666666666666", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format",
           return_value=("66666666-6666-4666-8666-666666666666", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_unknown_status_rejected_fail_closed(self, ms, mc, mf, mr):
        # Allowlist gate: any status outside {active,paused,waiting_input}
        # fails closed (council pass-2 dialectic — blocklist was fail-open).
        uid = "66666666-6666-4666-8666-666666666666"
        s = _mock_server({uid: _meta(status="quarantined", label="Q")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature",
                   return_value={"uuid": None}):
            u, e = require_registered_agent({"agent_id": uid})
        assert u is None
        assert "quarantined" in _parse_tc(e)["error"].lower()

    @patch("src.mcp_handlers.validators.validate_agent_id_reserved_names", return_value=("Old", None))
    @patch("src.mcp_handlers.validators.validate_agent_id_format", return_value=("Old", None))
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_archived_via_label_rejected(self, ms, mc, mf, mr):
        # Same gate via label-based lookup path, not UUID.
        s = _mock_server({"u-archived": _meta(status="archived", label="Old")})
        s.ensure_metadata_loaded = MagicMock()
        ms.return_value = s
        with patch("src.mcp_handlers.support.agent_auth.compute_agent_signature",
                   return_value={"uuid": None}):
            u, e = require_registered_agent({"agent_id": "Old"})
        assert u is None
        assert "archived" in _parse_tc(e)["error"].lower()


class TestVerifyAgentOwnership:
    @patch("src.mcp_handlers.shared.get_mcp_server")
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="a1")
    def test_match(self, mc, ms):
        ms.return_value = _mock_server()
        assert verify_agent_ownership("a1", {}) is True

    @patch("src.mcp_handlers.shared.get_mcp_server")
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="other")
    def test_no_match(self, mc, ms):
        ms.return_value = _mock_server({"other": _meta()})
        assert verify_agent_ownership("a1", {}) is False

    @patch("src.mcp_handlers.shared.get_mcp_server")
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    def test_no_binding(self, mc, ms):
        ms.return_value = _mock_server()
        assert verify_agent_ownership("a1", {}) is False

    @patch("src.mcp_handlers.shared.get_mcp_server")
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="bound")
    def test_agent_uuid_attr(self, mc, ms):
        ms.return_value = _mock_server({"bound": _meta(agent_uuid="target")})
        assert verify_agent_ownership("target", {}) is True

    @patch("src.mcp_handlers.shared.get_mcp_server")
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="op")
    def test_operator_label_no_longer_grants_access(self, mc, ms):
        """Caller-claimed label='operator' must not grant cross-agent access.

        The former allow_operator branch read self-claimed label/tag strings,
        which any agent could set at onboard to self-promote. Removed; ACL
        primitives that grant cross-agent access must be server-verified.
        """
        ms.return_value = _mock_server({"op": _meta(label="Operator")})
        assert verify_agent_ownership("other", {}) is False

    @patch("src.mcp_handlers.shared.get_mcp_server")
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value="op")
    def test_operator_tag_no_longer_grants_access(self, mc, ms):
        ms.return_value = _mock_server({"op": _meta(label="Bot", tags=["Operator"])})
        assert verify_agent_ownership("other", {}) is False

    @patch("src.mcp_handlers.context.get_context_agent_id", side_effect=Exception("x"))
    def test_exception_false(self, mc):
        assert verify_agent_ownership("a", {}) is False


class TestGenerateActionableFeedback:
    def test_healthy(self):
        fb = generate_actionable_feedback({"coherence": 0.9, "risk_score": 0.1, "regime": "stable", "updates": 5})
        assert all("drop" not in f.lower() for f in fb)

    def test_first_update_skip(self):
        fb = generate_actionable_feedback({"coherence": 0.3, "regime": "exploration", "updates": 1})
        assert all("coherence" not in f.lower() for f in fb)

    def test_low_coherence_exploration(self):
        fb = generate_actionable_feedback({"coherence": 0.2, "regime": "exploration", "updates": 5})
        assert any("exploration" in f.lower() or "hypothes" in f.lower() for f in fb)

    def test_coherence_drop(self):
        fb = generate_actionable_feedback({"coherence": 0.29, "regime": "exploration", "updates": 5}, previous_coherence=0.7)
        assert any("drop" in f.lower() for f in fb)

    def test_low_coherence_stable(self):
        fb = generate_actionable_feedback({"coherence": 0.5, "regime": "stable", "updates": 5})
        assert any("drift" in f.lower() or "plan" in f.lower() for f in fb)

    def test_drop_in_locked(self):
        fb = generate_actionable_feedback({"coherence": 0.5, "regime": "locked", "updates": 5}, previous_coherence=0.9)
        assert any("drop" in f.lower() or "disrupt" in f.lower() for f in fb)

    def test_high_risk(self):
        fb = generate_actionable_feedback({"risk_score": 0.8, "updates": 5})
        assert any("complex" in f.lower() or "break" in f.lower() for f in fb)

    def test_high_risk_void_basin(self):
        fb = generate_actionable_feedback({"risk_score": 0.8, "updates": 5},
                                          interpreted_state={"health": "degraded", "mode": "active", "basin": "void"})
        assert any("void" in f.lower() or "wrong thing" in f.lower() for f in fb)

    def test_moderate_risk_degraded(self):
        fb = generate_actionable_feedback({"risk_score": 0.6, "updates": 5},
                                          interpreted_state={"health": "degraded", "mode": "active", "basin": "normal"})
        assert any("checkpoint" in f.lower() for f in fb)

    def test_void_high_e_low_i(self):
        fb = generate_actionable_feedback({"void_active": True, "E": 0.9, "I": 0.3, "updates": 5})
        assert any("high energy" in f.lower() and "low integrity" in f.lower() for f in fb)

    def test_void_high_i_low_e(self):
        fb = generate_actionable_feedback({"void_active": True, "E": 0.3, "I": 0.9, "updates": 5})
        assert any("high integrity" in f.lower() and "low energy" in f.lower() for f in fb)

    def test_void_balanced(self):
        fb = generate_actionable_feedback({"void_active": True, "E": 0.5, "I": 0.5, "updates": 5})
        assert any("misaligned" in f.lower() or "void" in f.lower() for f in fb)

    def test_confusion(self):
        fb = generate_actionable_feedback({"updates": 5}, response_text="I'm not sure what to do")
        assert any("uncertainty" in f.lower() or "self-awareness" in f.lower() for f in fb)

    def test_stuck(self):
        fb = generate_actionable_feedback({"updates": 5}, response_text="I'm stuck")
        assert any("stuck" in f.lower() or "rubber duck" in f.lower() for f in fb)

    def test_overconfidence(self):
        fb = generate_actionable_feedback({"coherence": 0.4, "updates": 5}, response_text="definitely right")
        assert any("confidence" in f.lower() or "assumption" in f.lower() for f in fb)

    def test_convergent_low(self):
        fb = generate_actionable_feedback({"coherence": 0.3, "regime": "transition", "updates": 5}, task_type="convergent")
        assert any("convergent" in f.lower() or "focusing" in f.lower() for f in fb)

    def test_divergent_very_low(self):
        fb = generate_actionable_feedback({"coherence": 0.2, "regime": "transition", "updates": 5}, task_type="divergent")
        assert any("divergent" in f.lower() or "idea" in f.lower() for f in fb)

    def test_divergent_moderate_no_feedback(self):
        fb = generate_actionable_feedback({"coherence": 0.45, "regime": "transition", "updates": 5}, task_type="divergent")
        assert all("coherence" not in f for f in fb)


class TestEdgeCases:
    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_error_with_datetime_detail(self, ms):
        d = _parse_tc(error_response("x", details={"when": datetime(2026, 1, 1, 12, 0)}))
        assert d["success"] is False

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    def test_success_enum(self, mc, ms):
        class S(Enum):
            A = "active"
        assert _parse_tc(success_response({"s": S.A})[0])["s"] == "active"

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    def test_success_set(self, mc, ms):
        d = _parse_tc(success_response({"t": {"a", "b", "c"}})[0])
        assert sorted(d["t"]) == ["a", "b", "c"]

    def test_roundtrip(self):
        r = format_metrics_report({"E": 0.8, "I": 0.9, "S": 0.1, "V": -0.05, "coherence": 0.95}, "rt")
        t = format_metrics_text(r)
        assert "Agent: rt" in t and "coherence: 0.950" in t

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    def test_unknown_category(self, ms):
        assert _parse_tc(error_response("x", error_code="C", error_category="custom"))["error_category"] == "custom"

    @patch("src.mcp_handlers.support.agent_auth.compute_agent_signature", return_value={"uuid": None})
    @patch("src.mcp_handlers.context.get_context_agent_id", return_value=None)
    def test_empty_data(self, mc, ms):
        assert _parse_tc(success_response({})[0])["success"] is True

    def test_deeply_nested(self):
        data = {"l1": {"l2": {"l3": [datetime(2026, 1, 1), {"dt": date(2026, 6, 15)}]}}}
        r = _make_json_serializable(data)
        p = json.loads(json.dumps(r))
        assert p["l1"]["l2"]["l3"][0] == "2026-01-01T00:00:00"
        assert p["l1"]["l2"]["l3"][1]["dt"] == "2026-06-15"
