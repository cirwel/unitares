"""
Comprehensive tests for src/mcp_handlers/model_inference.py

Tests the handle_call_model tool handler with fully mocked external
API calls (OpenAI SDK).

IMPORTANT: The privacy parameter defaults to "local", which routes to Ollama
before any provider-specific logic. Tests for non-Ollama providers must
explicitly set privacy="cloud" or privacy="auto" to bypass the Ollama shortcut.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock
from typing import Dict, Any

import pytest

# Ensure project root is on sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Ensure OpenAI attribute exists on the module for patching even when
# the openai package is not installed (CI environment).
import src.mcp_handlers.support.model_inference as _mi
if not hasattr(_mi, 'OpenAI'):
    _mi.OpenAI = None


def _parse_text_content(result):
    """
    Parse a TextContent or list-of-TextContent response from a tool handler.
    Returns the parsed JSON dict from the text field.
    """
    if isinstance(result, list):
        item = result[0]
    else:
        item = result
    # TextContent has a .text attribute that is a JSON string
    text = item.text if hasattr(item, "text") else str(item)
    return json.loads(text)


def _make_mock_response(content="Test response", tokens=42, model="gemini-flash", reasoning=None):
    """Create a mock OpenAI-style chat completion response.

    Ollama's OpenAI-compat adapter adds a `reasoning` field for
    thinking-style models (gemma4, deepseek-r1, etc.). Tests that exercise
    the reasoning fallback set reasoning=<str>. Default None ensures
    `getattr(message, "reasoning", None) or ""` evaluates to "" so
    non-reasoning-path tests behave as before.
    """
    mock_message = MagicMock()
    mock_message.content = content
    mock_message.reasoning = reasoning

    mock_choice = MagicMock()
    mock_choice.message = mock_message

    mock_usage = MagicMock()
    mock_usage.total_tokens = tokens

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = mock_usage
    mock_response.model = model

    return mock_response


# =============================================================================
# Tests: OpenAI SDK unavailable
# =============================================================================

class TestOpenAIUnavailable:
    """Tests when OpenAI SDK is not installed."""

    @pytest.mark.asyncio
    async def test_returns_error_when_openai_not_available(self):
        """handle_call_model returns error when OPENAI_AVAILABLE is False."""
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({"prompt": "test"})

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert "OpenAI SDK required" in parsed["error"]
        assert parsed.get("error_code") == "DEPENDENCY_MISSING"


# =============================================================================
# Tests: Missing prompt
# =============================================================================

class TestMissingPrompt:
    """Tests for missing required arguments."""

    @pytest.mark.asyncio
    async def test_returns_error_when_prompt_missing(self):
        """handle_call_model returns error when prompt is not provided."""
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({})

        parsed = _parse_text_content(result)
        assert parsed["success"] is False


# =============================================================================
# Tests: Inference host registry
# =============================================================================

class TestInferenceHostRegistry:
    """Tests for list_inference_hosts and describe_inference_host."""

    @pytest.mark.asyncio
    async def test_list_inference_hosts_includes_active_and_placeholder_hosts(self):
        with patch("src.mcp_handlers.support.inference_registry._ollama_available", return_value=True), \
             patch("src.mcp_handlers.support.inference_registry._hf_token_present", return_value=False), \
             patch.dict(os.environ, {"UNITARES_LLM_MODEL": "test-local:latest"}, clear=False):
            from src.mcp_handlers.support.model_inference import handle_list_inference_hosts
            result = await handle_list_inference_hosts({})

        parsed = _parse_text_content(result)
        host_ids = {host["host_id"] for host in parsed["hosts"]}
        assert parsed["success"] is True
        assert parsed["schema"] == "unitares.inference_hosts.v0"
        assert host_ids == {
            "ollama:local",
            "hf:router",
            "codex:host-adapter",
            "claude:host-adapter",
        }
        assert parsed["count"] == 4
        assert next(h for h in parsed["hosts"] if h["host_id"] == "ollama:local")["models"] == [
            "test-local:latest"
        ]

    @pytest.mark.asyncio
    async def test_list_inference_hosts_can_hide_unconfigured_hosts(self):
        with patch("src.mcp_handlers.support.inference_registry._ollama_available", return_value=True), \
             patch("src.mcp_handlers.support.inference_registry._hf_token_present", return_value=False):
            from src.mcp_handlers.support.model_inference import handle_list_inference_hosts
            result = await handle_list_inference_hosts({"include_unconfigured": False})

        parsed = _parse_text_content(result)
        assert [host["host_id"] for host in parsed["hosts"]] == ["ollama:local"]
        assert parsed["count"] == 1

    @pytest.mark.asyncio
    async def test_list_inference_hosts_filters_provider_kind(self):
        with patch("src.mcp_handlers.support.inference_registry._hf_token_present", return_value=True):
            from src.mcp_handlers.support.model_inference import handle_list_inference_hosts
            result = await handle_list_inference_hosts({"provider_kind": "hf"})

        parsed = _parse_text_content(result)
        assert [host["host_id"] for host in parsed["hosts"]] == ["hf:router"]

    @pytest.mark.asyncio
    async def test_describe_inference_host_unknown_fails_closed(self):
        from src.mcp_handlers.support.model_inference import handle_describe_inference_host

        result = await handle_describe_inference_host({"host_id": "missing:host"})

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "INFERENCE_HOST_NOT_FOUND"


# =============================================================================
# Tests: Provider routing - Ollama (local / privacy=local)
# =============================================================================

class TestOllamaRouting:
    """Tests for Ollama (local) provider routing.

    Note: privacy defaults to "local", so Ollama is the default path
    unless privacy is explicitly set to something else.
    """

    @pytest.mark.asyncio
    async def test_privacy_local_routes_to_ollama(self):
        """privacy=local (the default) routes to Ollama."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            content="ollama response", model="llama3:70b"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "privacy": "local",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "ollama"
        assert parsed["response"] == "ollama response"

    @pytest.mark.asyncio
    async def test_default_privacy_routes_to_ollama(self):
        """Default privacy (no explicit value) routes to Ollama since default is 'local'."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            content="default ollama", model="llama3:70b"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "ollama"

    @pytest.mark.asyncio
    async def test_provider_ollama_routes_correctly(self):
        """provider=ollama routes to Ollama endpoint."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            content="ollama direct", model="llama3:70b"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance) as mock_openai:
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "ollama",
                "privacy": "cloud",  # must bypass default privacy=local
            })

        # Verify OpenAI was created with Ollama endpoint
        mock_openai.assert_called_once()
        call_kwargs = mock_openai.call_args
        assert "localhost:11434" in call_kwargs[1]["base_url"]
        assert call_kwargs[1]["api_key"] == "ollama"

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "ollama"

    @pytest.mark.asyncio
    async def test_ollama_uses_default_model_for_auto(self, monkeypatch):
        """Ollama with model=auto defaults to gemma4:latest (UNITARES_LLM_MODEL fallback)."""
        monkeypatch.delenv("UNITARES_LLM_MODEL", raising=False)
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="gemma4:latest"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "ollama",
                "model": "auto",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "gemma4:latest"

    @pytest.mark.asyncio
    async def test_ollama_auto_respects_unitares_llm_model_env(self, monkeypatch):
        """UNITARES_LLM_MODEL env var overrides the gemma4:latest default."""
        monkeypatch.setenv("UNITARES_LLM_MODEL", "qwen3-coder-next:latest")
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="qwen3-coder-next:latest"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "ollama",
                "model": "auto",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "qwen3-coder-next:latest"

    @pytest.mark.asyncio
    async def test_ollama_preserves_llama_3_1_8b_model(self):
        """llama-3.1-8b is NOT silently rewritten to llama3:70b (was a router bug)."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="llama-3.1-8b"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "privacy": "local",
                "model": "llama-3.1-8b",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "llama-3.1-8b"

    @pytest.mark.asyncio
    async def test_ollama_preserves_specified_model(self):
        """Ollama preserves a specific model name when not 'auto'."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="my-custom-model"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "privacy": "local",
                "model": "my-custom-model",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "my-custom-model"

    @pytest.mark.asyncio
    async def test_ollama_model_not_found_error_points_at_ollama_list(self):
        """Model-not-found recovery hint mentions `ollama list`, not stale model names."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.side_effect = Exception(
            "model 'nonexistent-model' not found"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
                "model": "nonexistent-model",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert parsed.get("error_code") == "MODEL_NOT_AVAILABLE"
        recovery_action = parsed["recovery"]["action"]
        assert "ollama list" in recovery_action
        assert "nonexistent-model" in recovery_action
        # Stale recommendations must be gone
        assert "qwen2.5:14b" not in recovery_action
        assert "llama-3.1-8b" not in recovery_action


# =============================================================================
# Tests: Provider routing - Hugging Face
# =============================================================================

class TestHuggingFaceRouting:
    """Tests for Hugging Face Inference Provider routing.

    All HF tests must use privacy="cloud" to bypass the default
    privacy="local" Ollama shortcut.
    """

    @pytest.mark.asyncio
    async def test_hf_provider_requires_token(self):
        """HF provider returns error when no token is set."""
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch.dict("os.environ", {}, clear=True):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "hf",
                "privacy": "cloud",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert "HF_TOKEN" in parsed["error"]
        assert parsed.get("error_code") == "MISSING_CONFIG"

    @pytest.mark.asyncio
    async def test_hf_provider_with_token(self):
        """HF provider works when token is set."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            content="hf response", model="deepseek-ai/DeepSeek-R1:fastest"
        )

        env = {"HF_TOKEN": "hf_test_token_123"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance) as mock_openai, \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "hf",
                "privacy": "cloud",
            })

        mock_openai.assert_called_once()
        call_kwargs = mock_openai.call_args[1]
        assert "router.huggingface.co" in call_kwargs["base_url"]
        assert call_kwargs["api_key"] == "hf_test_token_123"

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "huggingface"

    @pytest.mark.asyncio
    async def test_hf_strips_hf_prefix_from_model(self):
        """HF provider strips 'hf:' prefix from model name."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        env = {"HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "hf",
                "privacy": "cloud",
                "model": "hf:my-org/my-model",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        # Should strip "hf:" and add ":fastest"
        assert call_kwargs["model"] == "my-org/my-model:fastest"

    @pytest.mark.asyncio
    async def test_hf_model_with_colon_not_doubled(self):
        """HF provider does not add :fastest if model already has a colon."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        env = {"HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "hf",
                "privacy": "cloud",
                "model": "deepseek-ai/DeepSeek-R1:cheapest",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "deepseek-ai/DeepSeek-R1:cheapest"

    @pytest.mark.asyncio
    async def test_hf_auto_detection_by_model_prefix(self):
        """Auto-detects HF provider when model starts with deepseek-ai/."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        env = {"HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "auto",
                "model": "deepseek-ai/DeepSeek-V2",
                "privacy": "cloud",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "huggingface"

    @pytest.mark.asyncio
    async def test_hf_default_model_for_auto(self):
        """HF with model=auto defaults to deepseek-ai/DeepSeek-R1:fastest."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        env = {"HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "hf",
                "privacy": "cloud",
                "model": "auto",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "deepseek-ai/DeepSeek-R1:fastest"


# =============================================================================
# Tests: Gemini provider removed
# =============================================================================
# The Gemini provider was removed in fix/drop-gemini-provider. The router
# exposes only "ollama", "hf", and "auto" now. If Gemini support is restored
# later, restore TestGeminiRouting and routed_via="gemini-direct" coverage.


# =============================================================================
# Tests: Provider routing - Auto selection
# =============================================================================

class TestAutoProviderSelection:
    """Tests for auto provider selection logic.

    These tests use privacy="cloud" + provider="auto" to test the full
    auto-selection logic (Ollama check via socket, fallback chain).
    """

    @pytest.mark.asyncio
    async def test_auto_selects_ollama_when_available(self):
        """Auto provider prefers Ollama when it is running."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="gemma3:27b"
        )

        # Mock socket to simulate Ollama running
        mock_socket = MagicMock()
        mock_socket.connect_ex.return_value = 0  # Connected successfully

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch("socket.socket", return_value=mock_socket):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "auto",
                "privacy": "cloud",
                "model": "auto",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "ollama"

    @pytest.mark.asyncio
    async def test_auto_falls_back_to_hf_when_ollama_down(self):
        """Auto provider falls back to HF when Ollama is not running and HF_TOKEN is set."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="deepseek-ai/DeepSeek-R1:fastest"
        )

        mock_socket = MagicMock()
        mock_socket.connect_ex.return_value = 1  # No Ollama

        env = {"HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch("socket.socket", return_value=mock_socket), \
             patch.dict("os.environ", env, clear=True):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "auto",
                "privacy": "cloud",
                "model": "auto",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "huggingface"

    @pytest.mark.asyncio
    async def test_auto_ignores_google_key_when_set(self, monkeypatch):
        """GOOGLE_AI_API_KEY is not consulted by the auto fallback anymore."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="deepseek-ai/DeepSeek-R1:fastest"
        )

        mock_socket = MagicMock()
        mock_socket.connect_ex.return_value = 1  # No Ollama

        # Google key present but should be ignored; HF should win.
        monkeypatch.delenv("UNITARES_LLM_MODEL", raising=False)
        env = {"GOOGLE_AI_API_KEY": "ignored_key", "HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch("socket.socket", return_value=mock_socket), \
             patch.dict("os.environ", env, clear=True):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "auto",
                "privacy": "cloud",
                "model": "auto",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "huggingface"

    @pytest.mark.asyncio
    async def test_auto_returns_error_when_nothing_available(self):
        """Auto provider returns error when Ollama is down and HF_TOKEN absent.

        GOOGLE_AI_API_KEY no longer counts — Gemini was removed.
        """
        mock_socket = MagicMock()
        mock_socket.connect_ex.return_value = 1  # No Ollama

        # Even with GOOGLE_AI_API_KEY set, there's no provider available.
        env = {"GOOGLE_AI_API_KEY": "test_key"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("socket.socket", return_value=mock_socket), \
             patch.dict("os.environ", env, clear=True):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "auto",
                "privacy": "cloud",
                "model": "auto",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert "No provider available" in parsed["error"]
        assert "HF_TOKEN" in parsed["error"]


# =============================================================================
# Tests: Unknown provider rejection (Pydantic-blocked at MCP boundary,
# but direct callers must still get a clean error)
# =============================================================================

class TestUnknownProvider:
    """Tests that an unknown provider value returns INVALID_PROVIDER."""

    @pytest.mark.asyncio
    async def test_unknown_provider_rejected(self):
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "openai",
                "privacy": "cloud",
                "model": "gpt-4",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert parsed.get("error_code") == "INVALID_PROVIDER"
        assert "openai" in parsed["error"]


# =============================================================================
# Tests: Reasoning-field fallback (thinking-style models)
# =============================================================================

class TestReasoningFallback:
    """Tests for the Ollama/gemma4 empty-content + reasoning fallback.

    When a thinking-style model (gemma4, deepseek-r1) runs out of max_tokens
    while reasoning, `message.content` is empty but `message.reasoning` holds
    the trace. The router should surface the reasoning so callers see
    something instead of an empty string.
    """

    @pytest.mark.asyncio
    async def test_empty_content_with_reasoning_returns_reasoning(self):
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            content="",
            reasoning="Step 1: Analyze the problem. Step 2: ...",
            model="gemma4:latest",
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "ollama",
                "model": "gemma4:latest",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert "Step 1: Analyze the problem" in parsed["response"]
        # The fallback prepends a marker so callers can tell they got thinking,
        # not a final answer.
        assert "token limit" in parsed["response"].lower()

    @pytest.mark.asyncio
    async def test_empty_content_with_no_reasoning_returns_empty(self):
        """If both content and reasoning are empty, pass through empty (don't fabricate)."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            content="",
            reasoning=None,
            model="gemma4:latest",
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "ollama",
                "model": "gemma4:latest",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["response"] == ""

    @pytest.mark.asyncio
    async def test_content_present_reasoning_ignored(self):
        """When content is non-empty, reasoning is ignored (no concatenation)."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            content="The answer is 42.",
            reasoning="I considered many options before landing on 42.",
            model="gemma4:latest",
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "What is the answer?",
                "provider": "ollama",
                "model": "gemma4:latest",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["response"] == "The answer is 42."
        # Reasoning trace must not leak into the visible response when content
        # is the authoritative answer.
        assert "I considered many options" not in parsed["response"]


# =============================================================================
# Tests: Response content and energy cost
# =============================================================================

class TestResponseContent:
    """Tests for response content, tokens, and energy cost."""

    @pytest.mark.asyncio
    async def test_response_includes_all_fields(self):
        """Successful response includes all expected fields."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            content="The answer is 42", tokens=100, model="gemini-flash"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "What is the meaning of life?",
                "provider": "ollama",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["response"] == "The answer is 42"
        assert parsed["tokens_used"] == 100
        assert "model_used" in parsed
        assert "energy_cost" in parsed
        assert "routed_via" in parsed
        assert "task_type" in parsed
        assert parsed["inference"]["schema"] == "unitares.inference_result.v0"
        assert parsed["inference"]["host_id"] == "ollama:local"
        assert parsed["inference"]["provider_kind"] == "ollama"
        assert parsed["inference"]["model_used"] == parsed["model_used"]
        assert parsed["inference"]["tokens_used"] == parsed["tokens_used"]
        assert parsed["inference"]["prompt_hash"].startswith("sha256:")
        assert parsed["inference"]["response_hash"].startswith("sha256:")
        assert isinstance(parsed["inference"]["latency_ms"], int)
        assert "message" in parsed

    @pytest.mark.asyncio
    async def test_unavailable_host_id_fails_before_model_call(self):
        """Subscription-backed placeholders are visible but not callable."""
        mock_client_instance = MagicMock()

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "host_id": "claude:host-adapter",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert parsed["error_code"] == "INFERENCE_HOST_UNAVAILABLE"
        mock_client_instance.chat.completions.create.assert_not_called()

    # `test_energy_cost_free_tier_flash` removed: gemini-flash is no longer a
    # supported model after #80 dropped the Gemini provider; the free-tier
    # classifier now only matches llama/qwen/gemma. The llama variant below
    # exercises the same free-tier branch.

    @pytest.mark.asyncio
    async def test_energy_cost_free_tier_llama(self):
        """Free tier models (llama) get low energy cost."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="llama3:70b"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
                "model": "llama-3.1-8b",
            })

        parsed = _parse_text_content(result)
        assert parsed["energy_cost"] == 0.01

    # `test_energy_cost_pro_tier` removed: the 0.02 "pro tier" branch no
    # longer exists. After #80 dropped Gemini, the energy cost has two tiers:
    # free (0.01) for llama/qwen/gemma, default (0.03) otherwise. The default
    # tier is covered by `test_energy_cost_default_tier` below.

    @pytest.mark.asyncio
    async def test_energy_cost_default_tier(self):
        """Unknown models get default energy cost."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="custom-model"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
                "model": "custom-model",
            })

        parsed = _parse_text_content(result)
        assert parsed["energy_cost"] == 0.03

    @pytest.mark.asyncio
    async def test_handles_missing_usage_attribute(self):
        """Handles responses without usage attribute gracefully."""
        mock_response = _make_mock_response()
        del mock_response.usage  # Remove usage attribute

        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = mock_response

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["tokens_used"] == 0


# =============================================================================
# Tests: Parameter handling
# =============================================================================

class TestParameterHandling:
    """Tests for optional parameter parsing."""

    @pytest.mark.asyncio
    async def test_default_parameters(self):
        """Default parameters are applied when not specified."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })

        # Verify parameters passed to chat.completions.create
        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["max_tokens"] == 2048
        assert call_kwargs["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_custom_parameters(self):
        """Custom parameters override defaults."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
                "max_tokens": "1000",
                "temperature": "0.3",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["max_tokens"] == 1000
        assert call_kwargs["temperature"] == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_task_type_included_in_response(self):
        """task_type parameter is included in response."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
                "task_type": "analysis",
            })

        parsed = _parse_text_content(result)
        assert parsed["task_type"] == "analysis"

    @pytest.mark.asyncio
    async def test_prompt_passed_to_messages(self):
        """The prompt is correctly passed in messages to the API."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Analyze this code for bugs",
                "provider": "ollama",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["messages"] == [{"role": "user", "content": "Analyze this code for bugs"}]


# =============================================================================
# Tests: Error handling
# =============================================================================

class TestErrorHandling:
    """Tests for API call error handling."""

    @pytest.mark.asyncio
    async def test_timeout_error(self):
        """Timeout errors get specific error code."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.side_effect = Exception("Request timeout exceeded")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert parsed.get("error_code") == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_rate_limit_error(self):
        """Rate limit errors get specific error code."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.side_effect = Exception("Rate limit exceeded")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert parsed.get("error_code") == "RATE_LIMIT_EXCEEDED"

    @pytest.mark.asyncio
    async def test_model_not_found_error(self):
        """Model not found errors get specific error code."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.side_effect = Exception("Model not found: bad-model")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
                "model": "bad-model",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert parsed.get("error_code") == "MODEL_NOT_AVAILABLE"

    @pytest.mark.asyncio
    async def test_invalid_model_error(self):
        """'invalid' in error message triggers MODEL_NOT_AVAILABLE."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.side_effect = Exception("invalid model specified")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert parsed.get("error_code") == "MODEL_NOT_AVAILABLE"

    @pytest.mark.asyncio
    async def test_generic_error(self):
        """Generic errors get INFERENCE_ERROR code."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.side_effect = Exception("Something went wrong")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert parsed.get("error_code") == "INFERENCE_ERROR"

    @pytest.mark.asyncio
    async def test_error_includes_recovery_info(self):
        """Error responses include recovery guidance."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.side_effect = Exception("Something broke")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        assert "recovery" in parsed

    @pytest.mark.asyncio
    async def test_error_includes_model_details(self):
        """Error responses include model/base_url/task_type details."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.side_effect = Exception("Something broke")

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
                "task_type": "analysis",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is False
        # The error details include model_requested, base_url, task_type
        assert "task_type" in parsed or "model_requested" in parsed


# =============================================================================
# Tests: Routing via detection
# =============================================================================

class TestRoutingViaDetection:
    """Tests for routed_via field detection in response."""

    @pytest.mark.asyncio
    async def test_routed_via_ollama(self):
        """Detects ollama routing."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })

        parsed = _parse_text_content(result)
        assert parsed["routed_via"] == "ollama"

    @pytest.mark.asyncio
    async def test_routed_via_huggingface(self):
        """Detects huggingface routing."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        env = {"HF_TOKEN": "test"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "hf",
                "privacy": "cloud",
            })

        parsed = _parse_text_content(result)
        assert parsed["routed_via"] == "huggingface"

    # Gemini provider and ngrok.ai gateway (routed_via="gemini-direct"/"ngrok.ai")
    # were removed. The "direct" routed_via fallback is retained as defensive
    # default but is unreachable through the supported provider set.


# =============================================================================
# Tests: EISV Energy tracking
# =============================================================================

class TestEnergyTracking:
    """Tests for EISV Energy consumption tracking."""

    @pytest.mark.asyncio
    async def test_energy_tracking_with_agent_id(self):
        """When agent_id is provided, Energy tracking is attempted."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        mock_monitor = MagicMock()
        mock_mcp_server = MagicMock()
        mock_mcp_server.get_or_create_monitor.return_value = mock_monitor

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=mock_mcp_server):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
                "agent_id": "test-agent-123",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        # Monitor should have been called
        mock_mcp_server.get_or_create_monitor.assert_called_once_with("test-agent-123")
        mock_monitor.process_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_energy_tracking_failure_non_blocking(self):
        """Energy tracking failure does not block the response."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch("src.mcp_handlers.shared.get_mcp_server", side_effect=Exception("server not ready")):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
                "agent_id": "test-agent-123",
            })

        # Should still succeed despite tracking failure
        parsed = _parse_text_content(result)
        assert parsed["success"] is True

    @pytest.mark.asyncio
    async def test_no_energy_tracking_without_agent_id(self):
        """When no agent_id, Energy tracking is skipped gracefully."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "test",
                "provider": "ollama",
            })

        parsed = _parse_text_content(result)
        assert parsed["success"] is True


# =============================================================================
# Tests: Provider routing - Qwen
# =============================================================================

class TestQwenRouting:
    """Tests for Qwen model routing.

    Qwen models route through HF (cloud) or Ollama (local).
    All HF tests must use privacy="cloud" to bypass the Ollama shortcut.
    """

    @pytest.mark.asyncio
    async def test_qwen_hf_prefix_routes_to_huggingface(self):
        """Model starting with Qwen/ auto-routes to HF."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="Qwen/Qwen2.5-72B-Instruct:fastest"
        )

        env = {"HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance) as mock_openai, \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "auto",
                "model": "Qwen/Qwen2.5-72B-Instruct",
                "privacy": "cloud",
            })

        call_kwargs = mock_openai.call_args[1]
        assert "router.huggingface.co" in call_kwargs["base_url"]

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "huggingface"

    @pytest.mark.asyncio
    async def test_qwen_shorthand_expands_to_full_model(self):
        """Bare 'qwen' expands to Qwen/Qwen2.5-72B-Instruct:fastest."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="Qwen/Qwen2.5-72B-Instruct:fastest"
        )

        env = {"HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "hf",
                "model": "qwen",
                "privacy": "cloud",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "Qwen/Qwen2.5-72B-Instruct:fastest"

    @pytest.mark.asyncio
    async def test_qwen25_shorthand_also_expands(self):
        """Bare 'qwen2.5' expands to full HF model ID."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        env = {"HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "hf",
                "model": "qwen2.5",
                "privacy": "cloud",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "Qwen/Qwen2.5-72B-Instruct:fastest"

    @pytest.mark.asyncio
    async def test_qwen_preserves_explicit_model_with_suffix(self):
        """Explicit Qwen model with :cheapest suffix is not modified."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response()

        env = {"HF_TOKEN": "hf_test_token"}
        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance), \
             patch.dict("os.environ", env, clear=False):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "hf",
                "model": "Qwen/Qwen2.5-72B-Instruct:cheapest",
                "privacy": "cloud",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "Qwen/Qwen2.5-72B-Instruct:cheapest"

    @pytest.mark.asyncio
    async def test_qwen_ollama_passes_model_through(self):
        """Qwen model with provider=ollama passes model name through to Ollama."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="qwen2.5:14b"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "ollama",
                "model": "qwen2.5:14b",
            })

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "qwen2.5:14b"

        parsed = _parse_text_content(result)
        assert parsed["success"] is True
        assert parsed["routed_via"] == "ollama"

    @pytest.mark.asyncio
    async def test_qwen_energy_cost_free_tier(self):
        """Qwen models get free-tier energy cost (0.01)."""
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create.return_value = _make_mock_response(
            model="qwen2.5:14b"
        )

        with patch("src.mcp_handlers.support.model_inference.OPENAI_AVAILABLE", True), \
             patch("src.mcp_handlers.support.model_inference.OpenAI", return_value=mock_client_instance):
            from src.mcp_handlers.support.model_inference import handle_call_model
            result = await handle_call_model({
                "prompt": "Hello",
                "provider": "ollama",
                "model": "qwen2.5:14b",
            })

        parsed = _parse_text_content(result)
        assert parsed["energy_cost"] == 0.01
