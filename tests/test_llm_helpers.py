"""
Tests for src/mcp_handlers/llm_delegation.py - LLM delegation helpers.

Tests the pure sync helpers: _get_default_model.
"""

import pytest
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.support.llm_delegation import _get_default_model


# ============================================================================
# _get_default_model
# ============================================================================

class TestGetDefaultModel:

    def test_default_model(self, monkeypatch):
        """Without env var, should return gemma4:latest."""
        monkeypatch.delenv("UNITARES_MODEL_ID", raising=False)
        model = _get_default_model()
        assert model == "gemma4:latest"

    def test_env_override(self, monkeypatch):
        """UNITARES_MODEL_ID env var should override default."""
        monkeypatch.setenv("UNITARES_MODEL_ID", "llama3:70b")
        model = _get_default_model()
        assert model == "llama3:70b"

    def test_env_override_custom(self, monkeypatch):
        """Any string should be accepted as model name."""
        monkeypatch.setenv("UNITARES_MODEL_ID", "my-custom-model")
        model = _get_default_model()
        assert model == "my-custom-model"

    def test_returns_string(self, monkeypatch):
        monkeypatch.delenv("UNITARES_MODEL_ID", raising=False)
        result = _get_default_model()
        assert isinstance(result, str)
        assert len(result) > 0
