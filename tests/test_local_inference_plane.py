"""One local-inference plane, shared by every route.

Before this consolidation, ``llm_delegation.py`` (dialectic synthetic
reviewer, knowledge synthesis, check-in coaching) carried its own Ollama
client, its own default-model resolver, its own availability ping, and the
base URL was resolved differently per path: the structured dialectic route
honored ``UNITARES_OLLAMA_BASE`` while ``call_model`` and the OpenAI-compat
internal route hardcoded localhost — so setting the env var silently split
inference between two hosts. These tests pin the shared primitives in
``inference_registry`` as the single source: one base URL, one default model,
one cached availability probe, one provenance hash.

They also pin the boundary that stays deliberately UNshared: internal
delegation must not attribute Energy/EISV to the agent it is reasoning about
(the #1424 ``audit_only`` principle — instrumenting a surface must not enrol
it in a behavioral feed nobody measured).
"""

import inspect
from unittest.mock import patch

import pytest

from src.mcp_handlers.support import (
    delegated_inference,
    inference_registry,
    llm_delegation,
    model_inference,
)


BASE_OVERRIDE = "http://inference-box.lan:11500"


def test_one_base_url_for_every_local_route(monkeypatch):
    """UNITARES_OLLAMA_BASE must move the OpenAI-compat client, the native
    structured endpoint, AND the availability probe together — a base override
    that only some routes honor splits the plane between two hosts."""
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", BASE_OVERRIDE)

    assert inference_registry.ollama_base_url() == BASE_OVERRIDE
    assert llm_delegation._ollama_native_url() == BASE_OVERRIDE + "/api/chat"
    assert inference_registry._ollama_host_port() == ("inference-box.lan", 11500)

    client = llm_delegation._get_ollama_client()
    if client is not None:  # OpenAI SDK present
        assert str(client.base_url).rstrip("/") == BASE_OVERRIDE + "/v1"


def test_trailing_slash_and_default_port_are_normalized(monkeypatch):
    monkeypatch.setenv("UNITARES_OLLAMA_BASE", "http://10.0.0.5:11434/")
    assert inference_registry.ollama_base_url() == "http://10.0.0.5:11434"
    assert inference_registry._ollama_host_port() == ("10.0.0.5", 11434)

    monkeypatch.delenv("UNITARES_OLLAMA_BASE", raising=False)
    assert inference_registry.ollama_base_url() == "http://localhost:11434"
    assert inference_registry._ollama_host_port() == ("localhost", 11434)


def test_one_default_model_resolver(monkeypatch):
    """UNITARES_LLM_MODEL resolves through exactly one function; the internal
    lane must not keep a private copy that can drift from the registry's."""
    monkeypatch.setenv("UNITARES_LLM_MODEL", "qwen3:8b")
    assert inference_registry.default_local_model() == "qwen3:8b"
    # The internal lane's resolver IS the registry's, not a lookalike.
    assert llm_delegation._get_default_model is inference_registry.default_local_model
    # The host catalog advertises the same resolution.
    ollama_host = inference_registry.get_inference_host("ollama:local")
    assert ollama_host["models"] == ["qwen3:8b"]


@pytest.mark.asyncio
async def test_is_llm_available_uses_the_registry_probe():
    """Availability comes from the registry's TTL-cached probe — the same
    source call_model's auto-routing consults — not a private models.list ping."""
    with patch.object(llm_delegation, "OPENAI_AVAILABLE", True), \
         patch.object(llm_delegation, "_ollama_available", return_value=True) as probe:
        assert await llm_delegation.is_llm_available() is True
    probe.assert_called_once()

    with patch.object(llm_delegation, "OPENAI_AVAILABLE", True), \
         patch.object(llm_delegation, "_ollama_available", return_value=False):
        assert await llm_delegation.is_llm_available() is False


def test_one_provenance_hash_helper():
    """call_model and delegate_inference evidence hashing is the registry's
    single helper, not per-module copies that could format-drift."""
    assert model_inference._sha256_text is inference_registry.sha256_text
    assert delegated_inference._sha256_text is inference_registry.sha256_text
    assert inference_registry.sha256_text("x").startswith("sha256:")


def test_internal_delegation_never_attributes_energy():
    """The internal lane shares the transport plane but NOT the accounting: a
    call made ABOUT an agent (coaching, synthetic review) is not work done BY
    that agent, so llm_delegation must not reach the per-agent monitor. If this
    boundary should ever move, it is a measured governance decision, not a
    refactor side effect."""
    source = inspect.getsource(llm_delegation)
    assert "process_update" not in source
    assert "get_or_create_monitor" not in source
