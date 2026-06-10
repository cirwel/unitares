"""
Tests for pure helper functions in src/mcp_handlers/identity_v2.py.

Tests _generate_agent_id and _get_date_context (pure date/string functions).
"""

import pytest
import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.identity.handlers import (
    _generate_agent_id,
    _get_date_context,
    derive_session_key,
    create_continuity_token,
    resolve_continuity_token,
)
from src.mcp_handlers.identity.session import (
    extract_token_iat,
    extract_token_exp,
    _decode_token_payload,
)


# ============================================================================
# _get_date_context
# ============================================================================

class TestGetDateContext:

    def test_returns_dict(self):
        result = _get_date_context()
        assert isinstance(result, dict)

    def test_has_required_keys(self):
        result = _get_date_context()
        for key in ['full', 'short', 'compact', 'iso', 'iso_utc', 'year', 'month', 'weekday']:
            assert key in result, f"Missing key: {key}"

    def test_year_is_current(self):
        result = _get_date_context()
        assert result['year'] == datetime.now().strftime('%Y')

    def test_short_format(self):
        result = _get_date_context()
        # Should be YYYY-MM-DD
        assert len(result['short']) == 10
        assert result['short'][4] == '-'

    def test_compact_format(self):
        result = _get_date_context()
        # Should be YYYYMMDD
        assert len(result['compact']) == 8
        assert result['compact'].isdigit()

    def test_iso_utc_ends_with_z(self):
        result = _get_date_context()
        assert result['iso_utc'].endswith('Z')

    def test_full_contains_month_name(self):
        result = _get_date_context()
        months = ['January', 'February', 'March', 'April', 'May', 'June',
                  'July', 'August', 'September', 'October', 'November', 'December']
        assert any(m in result['full'] for m in months)

    def test_weekday_is_valid(self):
        result = _get_date_context()
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        assert result['weekday'] in days


# ============================================================================
# _generate_agent_id
# ============================================================================

class TestGenerateAgentId:

    def test_with_model_type_basic(self):
        result = _generate_agent_id(model_type="claude-opus-4-5")
        assert "Claude_Opus_4_5" in result
        assert datetime.now().strftime("%Y%m%d") in result

    def test_with_model_type_gemini(self):
        result = _generate_agent_id(model_type="gemini-pro")
        assert "Gemini_Pro" in result

    def test_with_model_type_dots(self):
        result = _generate_agent_id(model_type="gpt.4.turbo")
        assert "Gpt_4_Turbo" in result

    def test_with_model_type_underscores(self):
        result = _generate_agent_id(model_type="llama_3_70b")
        assert "Llama_3_70B" in result or "Llama_3_70b" in result

    def test_model_type_stripped(self):
        result = _generate_agent_id(model_type="  claude-opus-4-5  ")
        assert "Claude_Opus_4_5" in result

    def test_with_client_hint(self):
        result = _generate_agent_id(client_hint="cursor")
        assert result.startswith("cursor_")
        assert datetime.now().strftime("%Y%m%d") in result

    def test_client_hint_lowercased(self):
        result = _generate_agent_id(client_hint="Cursor")
        assert result.startswith("cursor_")

    def test_third_party_client_prefixed(self):
        result = _generate_agent_id(model_type="claude-opus-4-5", client_hint="cursor")
        assert "Cursor_Claude_Opus_4_5" in result

    def test_fallback_anon(self):
        result = _generate_agent_id()
        assert result.startswith("anon_")
        assert datetime.now().strftime("%Y%m%d") in result

    def test_unknown_client_hint_falls_back(self):
        result = _generate_agent_id(client_hint="unknown")
        assert result.startswith("anon_")

    def test_empty_client_hint_falls_back(self):
        result = _generate_agent_id(client_hint="")
        assert result.startswith("anon_")

    def test_none_model_with_valid_client(self):
        result = _generate_agent_id(model_type=None, client_hint="vscode")
        assert result.startswith("vscode_")

    def test_returns_string(self):
        result = _generate_agent_id()
        assert isinstance(result, str)
        assert len(result) > 0


class TestClientHintLeakRegression:
    """Regression tests for descriptor-as-identifier leak.

    A free-text client_hint must never leak into agent_id. Identifiers are
    structured handles; descriptors are display strings. The two layers must
    not bleed.

    Original bug: passing client_hint="Anthropic Claude, mobile app, dogfooding
    UX review" produced agent_id "Anthropic Claude, mobile app, dogfooding UX
    review_20260508" — a sentence, used as a primary key.
    """

    def test_dogfood_descriptor_does_not_become_identifier(self):
        bug_input = "Anthropic Claude, mobile app, dogfooding UX review"
        result = _generate_agent_id(client_hint=bug_input)
        assert bug_input not in result
        assert result.startswith("anon_")

    def test_long_hint_rejected(self):
        result = _generate_agent_id(client_hint="x" * 41)
        assert result.startswith("anon_")

    def test_hint_with_spaces_rejected(self):
        result = _generate_agent_id(client_hint="cursor with extra context")
        assert result.startswith("anon_")

    def test_hint_with_punctuation_rejected(self):
        result = _generate_agent_id(client_hint="cursor, v1.2.3")
        assert result.startswith("anon_")

    def test_valid_short_hints_still_work(self):
        for hint in ("cursor", "vscode", "claude_desktop", "claude-code", "chatgpt"):
            result = _generate_agent_id(client_hint=hint)
            assert result.startswith(f"{hint.lower()}_"), \
                f"Expected '{hint.lower()}_...' for hint {hint!r}, got {result!r}"

    def test_valid_hint_with_model_still_prefixes(self):
        result = _generate_agent_id(model_type="claude-opus-4-5", client_hint="cursor")
        assert "Cursor_Claude_Opus_4_5" in result

    def test_invalid_hint_with_model_uses_model_only(self):
        bug_input = "Anthropic Claude, mobile app"
        result = _generate_agent_id(model_type="claude-opus-4-5", client_hint=bug_input)
        assert bug_input not in result
        assert "Claude_Opus_4_5" in result
        assert not result.startswith("Anthropic")


# ============================================================================
# derive_session_key (async, signals=None uses context/stdio fallback)
# ============================================================================

class TestDeriveSessionKey:

    @pytest.mark.asyncio
    async def test_explicit_client_session_id(self):
        result = await derive_session_key(None, {"client_session_id": "my-session-123"})
        assert result == "my-session-123"

    @pytest.mark.asyncio
    async def test_explicit_client_session_id_scoped_by_model_type(self):
        result = await derive_session_key(None, {"client_session_id": "my-session-123", "model_type": "gpt-5-codex"})
        assert result == "my-session-123:gpt"

    @pytest.mark.asyncio
    async def test_explicit_takes_priority(self):
        """client_session_id should take priority over context."""
        result = await derive_session_key(None, {"client_session_id": "explicit-id"})
        assert result == "explicit-id"

    @pytest.mark.asyncio
    async def test_empty_client_session_id_falls_through(self):
        """Empty string is falsy, should fall through."""
        result = await derive_session_key(None, {"client_session_id": ""})
        # Should not be empty string, should fall through to other methods
        assert result != ""

    @pytest.mark.asyncio
    async def test_none_client_session_id_falls_through(self):
        result = await derive_session_key(None, {"client_session_id": None})
        assert result is not None
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_no_args_returns_stdio_fallback(self):
        """With no context set, should fall through to stdio fallback."""
        import os
        result = await derive_session_key(None, {})
        assert result == f"stdio:{os.getpid()}"

    @pytest.mark.asyncio
    async def test_mcp_session_id_from_context(self):
        """When mcp_session_id is set in context, use it."""
        from src.mcp_handlers.context import set_mcp_session_id, reset_mcp_session_id
        token = set_mcp_session_id("mcp-session-abc123")
        try:
            result = await derive_session_key(None, {})
            assert result == "mcp:mcp-session-abc123"
        finally:
            reset_mcp_session_id(token)

    @pytest.mark.asyncio
    async def test_context_session_key_fallback(self):
        """When context session_key is set, use it as fallback."""
        from src.mcp_handlers.context import set_session_context, reset_session_context
        token = set_session_context(session_key="ctx-key-456")
        try:
            result = await derive_session_key(None, {})
            assert result == "ctx-key-456"
        finally:
            reset_session_context(token)

    @pytest.mark.asyncio
    async def test_returns_string(self):
        result = await derive_session_key(None, {})
        assert isinstance(result, str)


class TestContinuityToken:

    def test_create_and_resolve_roundtrip(self):
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = create_continuity_token(
                "11111111-2222-3333-4444-555555555555",
                "agent-111111111111:gpt",
                model_type="gpt-5-codex",
                client_hint="chatgpt",
            )
            assert token is not None
            resolved = resolve_continuity_token(token, model_type="gpt-5-codex")
            assert resolved == "agent-111111111111:gpt"

    def test_resolve_fails_on_model_mismatch(self):
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = create_continuity_token(
                "11111111-2222-3333-4444-555555555555",
                "agent-111111111111:claude",
                model_type="claude-opus-4-5",
                client_hint="claude_desktop",
            )
            assert resolve_continuity_token(token, model_type="gpt-5-codex") is None


class TestTokenPayloadAccessors:
    """`_decode_token_payload`, `extract_token_iat`, `extract_token_exp` share a single
    HMAC verification — these tests pin both the shared shape and the per-claim accessors.
    """

    AGENT_UUID = "11111111-2222-3333-4444-555555555555"
    SESSION_ID = "agent-decode-test:claude"

    def _make_token(self):
        return create_continuity_token(
            self.AGENT_UUID,
            self.SESSION_ID,
            model_type="claude-opus-4-5",
            client_hint="claude_desktop",
        )

    def test_decode_payload_returns_full_dict(self):
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = self._make_token()
            payload = _decode_token_payload(token)
            assert isinstance(payload, dict)
            assert payload["aid"] == self.AGENT_UUID
            assert payload["sid"] == self.SESSION_ID
            assert isinstance(payload["iat"], int)
            assert isinstance(payload["exp"], int)
            assert payload["exp"] > payload["iat"]

    def test_decode_payload_rejects_bad_signature(self):
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = self._make_token()
            assert token is not None
            tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
            assert _decode_token_payload(tampered) is None

    def test_decode_payload_rejects_wrong_version(self):
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = self._make_token()
            assert token is not None
            _, payload_b64, sig_b64 = token.split(".", 2)
            forged = f"v9.{payload_b64}.{sig_b64}"
            assert _decode_token_payload(forged) is None

    def test_decode_payload_returns_none_without_secret(self):
        with patch.dict("os.environ", {}, clear=True):
            assert _decode_token_payload("v1.aaa.bbb") is None

    def test_extract_iat_matches_decode(self):
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = self._make_token()
            payload = _decode_token_payload(token)
            assert extract_token_iat(token) == payload["iat"]

    def test_extract_exp_matches_decode(self):
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = self._make_token()
            payload = _decode_token_payload(token)
            assert extract_token_exp(token) == payload["exp"]

    def test_extract_exp_does_not_check_expiry(self):
        """Symmetric with extract_token_iat: returns exp claim even on expired tokens
        so callers can compute lifetime/observed-staleness telemetry."""
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            token = create_continuity_token(
                self.AGENT_UUID,
                self.SESSION_ID,
                model_type="claude-opus-4-5",
                ttl_seconds=60,
            )
            assert resolve_continuity_token(token, model_type="claude-opus-4-5") == self.SESSION_ID
            # Past the exp boundary, resolve_continuity_token rejects but exp accessor still reports.
            import time
            with patch("src.mcp_handlers.identity.session.time.time", return_value=time.time() + 3600):
                assert resolve_continuity_token(token, model_type="claude-opus-4-5") is None
                assert extract_token_exp(token) is not None

    def test_extract_iat_returns_none_for_garbage(self):
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            assert extract_token_iat("not-a-token") is None
            assert extract_token_iat("") is None
            assert extract_token_iat(None) is None  # type: ignore[arg-type]

    def test_extract_exp_returns_none_for_garbage(self):
        with patch.dict("os.environ", {"UNITARES_CONTINUITY_TOKEN_SECRET": "test-secret"}, clear=False):
            assert extract_token_exp("not-a-token") is None
            assert extract_token_exp("") is None
            assert extract_token_exp(None) is None  # type: ignore[arg-type]
