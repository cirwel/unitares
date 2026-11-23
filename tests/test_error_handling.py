"""Regression tests for src/mcp_handlers/error_handling.py.

This is the error-response surface every handler funnels failures through. Two
pieces carry real, security-relevant logic that was untested:

  * _sanitize_error_message strips file paths, stack traces, and internal
    module qualifications *before the text reaches a client* — its whole job is
    to prevent internal-structure leakage, so a regression that stops stripping
    is a security regression.
  * _infer_error_code_and_category maps free-text messages to a stable
    (code, category) contract, with a deliberately ordered pattern list (the
    timeout-before-session ordering is load-bearing — see the inline comment
    about list_dialectic_sessions).

These tests pin both, plus the error_response envelope shape.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.mcp_handlers.error_handling import (
    _infer_error_code_and_category,
    _sanitize_error_message,
    error_response,
)


# --------------------------------------------------------------------------- #
# _infer_error_code_and_category
# --------------------------------------------------------------------------- #

class TestInferErrorCode:
    @pytest.mark.parametrize(
        "message, code, category",
        [
            ("Agent not found", "NOT_FOUND", "validation_error"),
            ("missing required parameter agent_id", "MISSING_REQUIRED", "validation_error"),
            ("value already exists", "ALREADY_EXISTS", "validation_error"),
            ("Request timed out", "TIMEOUT", "system_error"),
            ("permission denied", "PERMISSION_DENIED", "auth_error"),
            ("agent is paused", "AGENT_PAUSED", "state_error"),
            ("database connection lost", "CONNECTION_ERROR", "system_error"),
        ],
    )
    def test_pattern_maps_to_expected_code(self, message, code, category):
        assert _infer_error_code_and_category(message) == (code, category)

    def test_unmatched_message_returns_none(self):
        assert _infer_error_code_and_category("zxqw plover gibberish") == (None, None)

    def test_timeout_wins_over_session_substring(self):
        # The pattern order is load-bearing: a timeout on a tool whose name
        # contains 'session' must classify as TIMEOUT/system, not SESSION/auth.
        code, category = _infer_error_code_and_category(
            "Request timed out calling list_dialectic_sessions")
        assert code == "TIMEOUT"
        assert category == "system_error"

    def test_validation_wins_over_auth(self):
        # 'invalid' (validation) precedes 'session' (auth) in the pattern list.
        code, category = _infer_error_code_and_category("invalid session token")
        assert code == "INVALID_PARAM"
        assert category == "validation_error"

    def test_is_required_classifies_as_validation_not_auth(self):
        # Regression: "session_id is required" is a missing-parameter error, but
        # the message contains the substring 'session', which would fall through
        # to the broad SESSION/auth pattern unless "is required" is caught as a
        # MISSING_REQUIRED validation error first. The "X is required" phrasing
        # (used by the dialectic submit handlers) must not look like an auth error.
        code, category = _infer_error_code_and_category("session_id is required")
        assert code == "MISSING_REQUIRED"
        assert category == "validation_error"


# --------------------------------------------------------------------------- #
# _sanitize_error_message
# --------------------------------------------------------------------------- #

class TestSanitizeErrorMessage:
    def test_non_string_coerced(self):
        assert _sanitize_error_message(12345) == "12345"

    def test_strips_absolute_path_keeps_filename(self):
        out = _sanitize_error_message("boom in /home/user/unitares/src/foo.py now")
        assert "/home/user" not in out
        assert "foo.py" in out

    def test_line_numbers_generalized(self):
        out = _sanitize_error_message("failure at line 4321 somewhere")
        assert "line 4321" not in out
        assert "line N" in out

    def test_traceback_removed_but_final_error_kept(self):
        msg = (
            "Traceback (most recent call last):\n"
            '  File "/x/y.py", line 10, in f\n'
            "    raise ValueError('boom')\n"
            "ValueError: boom"
        )
        out = _sanitize_error_message(msg)
        assert "Traceback" not in out
        assert "boom" in out

    def test_internal_module_qualification_stripped(self):
        out = _sanitize_error_message("src.mcp_handlers.identity.handler exploded")
        assert "src.mcp_handlers" not in out
        assert "exploded" in out

    def test_long_message_truncated_to_limit(self):
        # No sentence boundary → hard truncation with ellipsis. Limit is 500.
        out = _sanitize_error_message("x" * 600)
        assert len(out) <= 503
        assert out.endswith("...")

    def test_truncates_at_sentence_boundary_when_available(self):
        body = "First sentence is short. " + "y" * 600
        out = _sanitize_error_message(body)
        assert out.endswith(".")
        assert "First sentence is short." in out

    @pytest.mark.parametrize(
        "raw, expected, forbidden",
        [
            (
                "Upstream failed: https://api.example.com/v1/chat?api_key=sk-secret",
                "Upstream failed: https://api.example.com/v1/chat",
                ("api_key", "sk-secret"),
            ),
            (
                "Redirect to https://app.example.com/callback#access_token=tok123&state=xyz failed",
                "Redirect to https://app.example.com/callback failed",
                ("access_token", "tok123", "state=xyz"),
            ),
            (
                "GET https://api.example.com/v1/chat?api_key=secret#frag failed",
                "GET https://api.example.com/v1/chat failed",
                ("api_key", "secret", "#frag"),
            ),
            (
                "DB error on postgres://alice:p@ss@db.internal:5432/gov?password=pw&sslmode=require#sec",
                "DB error on postgres://db.internal:5432/gov",
                ("alice", "p@ss", "password", "sslmode", "#sec"),
            ),
            (
                "Request to https://api.example.com?token=abc== failed",
                "Request to https://api.example.com failed",
                ("token", "abc=="),
            ),
            (
                "Failed calling (https://api.example.com/v1/chat?jwt=eyJ.abc.def).",
                "Failed calling (https://api.example.com/v1/chat).",
                ("jwt", "eyJ", "abc"),
            ),
            (
                "Client error for url 'https://api.example.com/v1/chat?api_key=secret', retrying.",
                "Client error for url 'https://api.example.com/v1/chat', retrying.",
                ("api_key", "secret"),
            ),
            (
                'Payload {"url":"https://api.example.com/v1/chat?api_key=secret","status":500}',
                'Payload {"url":"https://api.example.com/v1/chat","status":500}',
                ("api_key", "secret"),
            ),
            (
                "See <https://api.example.com/items?ids=1,2&key=secret>; retrying.",
                "See <https://api.example.com/items>; retrying.",
                ("ids=", "key=secret"),
            ),
        ],
    )
    def test_strips_url_query_and_fragment(self, raw, expected, forbidden):
        out = _sanitize_error_message(raw)
        assert out == expected
        for secret in forbidden:
            assert secret not in out

    @pytest.mark.parametrize(
        "msg",
        [
            "Is it ready? Try again.",
            "See #1234 for details? Yes.",
            "Failed calling https://api.example.com/v1/chat. Is it ready? See #42.",
            "Did it reach https://api.example.com/health? Try again.",
        ],
    )
    def test_preserves_non_url_questions_and_hashes(self, msg):
        assert _sanitize_error_message(msg) == msg


# --------------------------------------------------------------------------- #
# error_response envelope
# --------------------------------------------------------------------------- #

class TestErrorResponse:
    def _payload(self, *args, **kwargs):
        tc = error_response(*args, **kwargs)
        return json.loads(tc.text)

    def test_basic_envelope_shape(self):
        d = self._payload("Agent not found", arguments=None)
        assert d["success"] is False
        assert d["error"] == "Agent not found"
        assert "server_time" in d
        assert "agent_signature" in d

    # -- the error path has no response_mode, so the envelope itself must
    # -- carry its own weight (dogfood 2026-08-24)

    @patch("src.mcp_handlers.context.get_context_client_hint")
    @patch("src.mcp_handlers.context.get_context_agent_id")
    @patch("src.mcp_handlers.shared.get_mcp_server")
    def test_error_signature_stubs_provenance_but_keeps_the_contract(
        self, mock_srv, mock_ctx, mock_hint
    ):
        """A bound caller's validation error carries the trimmed envelope.

        This has to mock a bound identity to mean anything: with no context
        agent_id, compute_agent_signature returns {"uuid": None} and there is
        no identity_context to assert about at all -- a version of this test
        that skipped the mocks would pass with the change fully reverted.
        """
        mock_ctx.return_value = "uuid-err"
        # A harness type has to be present, or runtime_provenance is stubbed by
        # #1872's "nothing was reported" branch and this test would pass with
        # the change reverted.
        mock_hint.return_value = "claude_code"
        server = MagicMock()
        server.agent_metadata = {
            "uuid-err": SimpleNamespace(
                status="active",
                label="dogfood",
                structured_id=None,
                display_name="dogfood",
                paused_at=None,
                agent_uuid="uuid-err",
                tags=[],
                public_agent_id="Claude_20260824",
            )
        }
        mock_srv.return_value = server

        d = self._payload("Invalid limit -5: must be at least 1", arguments=None)
        assert d["success"] is False
        signature = d["agent_signature"]
        # Guard the guard: if the identity never bound, the assertions below
        # would be vacuous.
        assert signature["uuid"] == "uuid-err"
        context = signature["identity_context"]

        assert context["agent_id_is"] == "public_structured_handle"
        assert context["public_handle"]["agent_id"] == "Claude_20260824"
        provenance = context["harness_context"]["runtime_provenance"]
        assert context["harness_context"]["harness_type"] == "claude_code"
        assert provenance["detail"] == "omitted"
        assert "model" not in provenance
        assert "identity_assurance" not in context
        assert "identity_assurance" in signature

    def test_auto_infers_code_and_category(self):
        d = self._payload("Agent not found", arguments=None)
        assert d["error_code"] == "NOT_FOUND"
        assert d["error_category"] == "validation_error"

    def test_explicit_code_not_overridden(self):
        d = self._payload("Agent not found", error_code="CUSTOM",
                          error_category="auth_error", arguments=None)
        assert d["error_code"] == "CUSTOM"
        assert d["error_category"] == "auth_error"

    def test_message_is_sanitized_in_envelope(self):
        d = self._payload("crash in /secret/path/to/module.py", arguments=None)
        assert "/secret/path" not in d["error"]
        assert "module.py" in d["error"]

    def test_string_details_are_sanitized(self):
        d = self._payload("bad", details={"hint": "see /home/op/x.py"},
                          arguments=None)
        assert "/home/op" not in d["hint"]
        assert "x.py" in d["hint"]

    def test_url_query_and_fragment_sanitized_in_message_and_details(self):
        d = self._payload(
            "failed calling https://user:pw@api.example.com/v1/chat?api_key=sk-1#frag",
            details={"endpoint": "https://api.example.com/v1/models?token=tok-2#top"},
            arguments=None,
        )
        assert d["error"] == "failed calling https://api.example.com/v1/chat"
        assert d["endpoint"] == "https://api.example.com/v1/models"
        for secret in ("user:pw", "api_key", "sk-1", "#frag", "token", "tok-2", "#top"):
            assert secret not in json.dumps(d)
