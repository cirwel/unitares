"""
Tests for src/mcp_handlers/naming_helpers.py - Agent naming utilities.

Uses os.environ patching for interface detection tests.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.support.naming_helpers import (
    detect_interface_context,
    disambiguate_public_handle,
    generate_name_suggestions,
    generate_structured_id,
    format_naming_guidance,
)


# ============================================================================
# detect_interface_context
# ============================================================================

class TestDetectInterfaceContext:

    def test_default(self):
        with patch.dict("os.environ", {}, clear=True):
            ctx = detect_interface_context()
            assert ctx["interface"] == "mcp_client"
            assert ctx["model_hint"] is None
            assert ctx["environment"] is None

    def test_cursor(self):
        with patch.dict("os.environ", {"CURSOR_PID": "12345"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["interface"] == "cursor"

    def test_vscode(self):
        with patch.dict("os.environ", {"VSCODE_PID": "12345"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["interface"] == "vscode"

    def test_claude_desktop(self):
        with patch.dict("os.environ", {"CLAUDE_DESKTOP": "1"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["interface"] == "claude_desktop"

    def test_override(self):
        with patch.dict("os.environ", {"GOVERNANCE_AGENT_PREFIX": "custom"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["interface"] == "custom"

    def test_anthropic_model_hint(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-123"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["model_hint"] == "claude"

    def test_openai_model_hint(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-123"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["model_hint"] == "gpt"

    def test_gemini_model_hint(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "key"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["model_hint"] == "gemini"

    def test_google_ai_model_hint(self):
        with patch.dict("os.environ", {"GOOGLE_AI_API_KEY": "key"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["model_hint"] == "gemini"

    def test_ci_environment(self):
        with patch.dict("os.environ", {"CI": "true"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["environment"] == "ci"

    def test_test_environment(self):
        with patch.dict("os.environ", {"TEST": "1"}, clear=True):
            ctx = detect_interface_context()
            assert ctx["environment"] == "test"


# ============================================================================
# generate_name_suggestions
# ============================================================================

class TestGenerateNameSuggestions:

    def test_returns_list(self):
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        result = generate_name_suggestions(context=ctx)
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_with_purpose(self):
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        result = generate_name_suggestions(context=ctx, purpose="debug auth")
        names = [s["name"] for s in result]
        assert any("debug" in n for n in names)

    def test_with_model_hint(self):
        ctx = {"interface": "cursor", "model_hint": "claude", "environment": None}
        result = generate_name_suggestions(context=ctx)
        names = [s["name"] for s in result]
        assert any("claude" in n for n in names)

    def test_collision_avoidance(self):
        ctx = {"interface": "mcp_client", "model_hint": None, "environment": None}
        result = generate_name_suggestions(context=ctx)
        # Get the first name and pass it as existing
        first_name = result[0]["name"]
        result2 = generate_name_suggestions(context=ctx, existing_names=[first_name])
        names2 = [s["name"] for s in result2]
        # The colliding name should be adjusted
        assert first_name not in names2 or any("_1" in n for n in names2)

    def test_max_four(self):
        ctx = {"interface": "cursor", "model_hint": "claude", "environment": None}
        result = generate_name_suggestions(context=ctx, purpose="test")
        assert len(result) <= 4

    def test_required_fields(self):
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        result = generate_name_suggestions(context=ctx)
        for sug in result:
            assert "name" in sug
            assert "description" in sug
            assert "rationale" in sug

    def test_none_context(self):
        """Should auto-detect context when None."""
        with patch.dict("os.environ", {}, clear=True):
            result = generate_name_suggestions(context=None)
            assert isinstance(result, list)


# ============================================================================
# generate_structured_id
# ============================================================================

class TestGenerateStructuredId:

    def test_basic(self):
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        result = generate_structured_id(context=ctx)
        assert "cursor" in result
        assert len(result) > 0

    def test_with_model_type(self):
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        result = generate_structured_id(context=ctx, model_type="claude-3.5-sonnet")
        assert "claude" in result

    def test_gemini_simplification(self):
        ctx = {"interface": "mcp", "model_hint": None, "environment": None}
        result = generate_structured_id(context=ctx, model_type="gemini-2.0-pro")
        assert "gemini" in result

    def test_gpt_simplification(self):
        ctx = {"interface": "mcp", "model_hint": None, "environment": None}
        result = generate_structured_id(context=ctx, model_type="gpt-4o")
        assert "gpt" in result

    def test_llama_simplification(self):
        ctx = {"interface": "mcp", "model_hint": None, "environment": None}
        result = generate_structured_id(context=ctx, model_type="llama-3.1-70b")
        assert "llama" in result

    def test_client_hint(self):
        ctx = {"interface": "mcp_client", "model_hint": None, "environment": None}
        result = generate_structured_id(context=ctx, client_hint="chatgpt")
        assert "chatgpt" in result

    def test_reserved_client_hint_does_not_leak_reserved_prefix(self):
        """A free-text client_hint that collides with a reserved prefix root
        (e.g. "admin") must NOT seed the structured id — otherwise the mint is a
        reserved-prefix agent_id that downstream validation rejects (KG dogfood
        2026-05-09 'client_hint leaks into agent_id namespace'). The id falls
        back to the detected interface instead."""
        from src.mcp_handlers.validators import validate_agent_id_reserved_names
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        for reserved in ("admin", "mcp", "root", "system", "governance", "auth", "admin-tools"):
            result = generate_structured_id(context=ctx, client_hint=reserved)
            assert result.startswith("cursor_"), f"{reserved!r} leaked: {result}"
            # The generated id must pass the reserved-name guard.
            _, err = validate_agent_id_reserved_names(result)
            assert err is None, f"{reserved!r} produced reserved id: {result}"

    def test_non_reserved_client_hint_still_used(self):
        """Ordinary client hints are unaffected — they still seed the id."""
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        assert generate_structured_id(context=ctx, client_hint="claude_code").startswith("claude_code_")
        assert "vscode" in generate_structured_id(context=ctx, client_hint="vscode")

    def test_collision_avoidance_single(self):
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        first = generate_structured_id(context=ctx)
        second = generate_structured_id(context=ctx, existing_ids=[first])
        assert second != first
        assert "_2" in second

    def test_collision_avoidance_multiple(self):
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        first = generate_structured_id(context=ctx)
        second = f"{first}_2"
        third = generate_structured_id(context=ctx, existing_ids=[first, second])
        assert third != first
        assert third != second
        assert "_3" in third

    def test_no_collision(self):
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        result = generate_structured_id(context=ctx, existing_ids=["other_id"])
        # No counter suffix should be added since there's no collision
        assert not result.endswith("_2")
        assert not result.endswith("_3")

    def test_removes_client_suffix(self):
        ctx = {"interface": "mcp_client", "model_hint": None, "environment": None}
        result = generate_structured_id(context=ctx)
        assert "_client" not in result
        assert "mcp" in result

    def test_agent_uuid_appends_uuid8_fragment(self):
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        result = generate_structured_id(
            context=ctx, agent_uuid="a4be406c-1234-5678-9abc-def012345678"
        )
        assert result.endswith("_a4be406c")
        assert "cursor" in result  # bucketable prefix preserved

    def test_agent_uuid_disambiguates_same_model_same_day(self):
        """Two agents with identical context but different UUIDs must not
        collapse onto one structured id — this is the duplicate-id fix."""
        ctx = {"interface": "claude_code", "model_hint": None, "environment": None}
        first = generate_structured_id(
            context=ctx, model_type="claude", agent_uuid="11111111-aaaa-bbbb-cccc-dddddddddddd"
        )
        second = generate_structured_id(
            context=ctx, model_type="claude", agent_uuid="22222222-aaaa-bbbb-cccc-dddddddddddd"
        )
        assert first != second
        assert first.endswith("_11111111")
        assert second.endswith("_22222222")

    def test_no_agent_uuid_keeps_legacy_bucket_format(self):
        """Callers with no UUID in scope keep the legacy collision-counter
        behavior (no uuid8 fragment appended)."""
        ctx = {"interface": "cursor", "model_hint": None, "environment": None}
        result = generate_structured_id(context=ctx)
        # Legacy format is {interface}_{date} with no trailing uuid8 block.
        assert result.startswith("cursor_")
        # Trailing token is the 8-digit date, not a hex uuid fragment.
        assert result.split("_")[-1].isdigit()


# ============================================================================
# disambiguate_public_handle
# ============================================================================

class TestDisambiguatePublicHandle:
    """The display layer must render distinct agents distinctly even when they
    share the non-unique {Model}_{date} public_agent_id bucket — this is the
    'agent duplication according to dashboard' fix."""

    def test_two_same_bucket_agents_render_distinctly(self):
        first = disambiguate_public_handle(
            "Claude_20260602", None, "cc447979-aaaa-bbbb-cccc-dddddddddddd"
        )
        second = disambiguate_public_handle(
            "Claude_20260602", None, "3cec10f9-aaaa-bbbb-cccc-dddddddddddd"
        )
        assert first == "Claude_20260602_cc447979"
        assert second == "Claude_20260602_3cec10f9"
        assert first != second

    def test_keeps_greppable_bucket_prefix(self):
        handle = disambiguate_public_handle(
            "Claude_20260602", None, "cc447979-aaaa-bbbb-cccc-dddddddddddd"
        )
        assert handle.startswith("Claude_20260602")

    def test_no_double_append_when_suffix_present(self):
        handle = disambiguate_public_handle(
            "Claude_20260602_cc447979", None, "cc447979-aaaa-bbbb-cccc-dddddddddddd"
        )
        assert handle == "Claude_20260602_cc447979"

    def test_falls_back_to_structured_id_when_no_bucket(self):
        # structured_id is already uuid8-suffixed by generate_structured_id.
        handle = disambiguate_public_handle(
            None, "claude_code_claude_20260602_cc447979", "cc447979-x"
        )
        assert handle == "claude_code_claude_20260602_cc447979"

    def test_no_uuid_keeps_bucket_unchanged(self):
        handle = disambiguate_public_handle("Claude_20260602", None, None)
        assert handle == "Claude_20260602"

    def test_non_uuid_identifier_adds_no_suffix(self):
        # Placeholder/non-UUID ids (e.g. "agent-1") must not contribute a
        # garbage fragment like "_agent" — regression for the CI failure where
        # require_agent_id returned the placeholder "agent-1".
        assert (
            disambiguate_public_handle("Gpt_5_Codex_20260404", None, "agent-1")
            == "Gpt_5_Codex_20260404"
        )

    def test_redacted_agent_prefix_uses_hex_tail(self):
        # The "agent-<hex>" redacted form disambiguates on its hex tail, not
        # on the literal "agent" token.
        handle = disambiguate_public_handle(
            "Claude_20260602", None, "agent-1234abcd5678"
        )
        assert handle == "Claude_20260602_1234abcd"

    def test_returns_none_when_nothing_available(self):
        assert disambiguate_public_handle(None, None, "cc447979-x") is None


# ============================================================================
# format_naming_guidance
# ============================================================================

class TestFormatNamingGuidance:

    def test_basic_structure(self):
        suggestions = [{"name": "test_name", "description": "test", "rationale": "test"}]
        result = format_naming_guidance(suggestions)
        assert "message" in result
        assert "suggestions" in result
        assert "how_to" in result
        assert "examples" in result
        assert "tips" in result

    def test_with_uuid(self):
        suggestions = []
        result = format_naming_guidance(suggestions, current_uuid="abcdef1234567890abcdef1234567890")
        assert "current_uuid" in result
        assert "note" in result
        assert result["current_uuid"].endswith("...")

    def test_without_uuid(self):
        suggestions = []
        result = format_naming_guidance(suggestions)
        assert "current_uuid" not in result

    def test_suggestions_passed(self):
        suggestions = [
            {"name": "test_1", "description": "d1", "rationale": "r1"},
            {"name": "test_2", "description": "d2", "rationale": "r2"},
        ]
        result = format_naming_guidance(suggestions)
        assert len(result["suggestions"]) == 2
